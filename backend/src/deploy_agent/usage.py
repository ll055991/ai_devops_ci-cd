"""LLM token 用量账本（M2）。

- llm_usage 表按 thread × 模型调用记一行：thread_id / 专员 / 模型 /
  input_tokens / output_tokens / total_tokens / created_at（id 顺序即轮次）
- UsageCallback 挂在模型实例上（主模型 + 各专员独立实例），aon_llm_end
  从调用结果的 token_usage 写库；thread_id 经每请求 ContextVar 传入
- 只记 tokens，不换算金额；写失败只记日志，不阻断主流程

实现方式参考 audit.UsageStore（aiosqlite 单连接 + 惰性建表 + 幂等 close）。
"""

from __future__ import annotations

import contextvars
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiosqlite
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from loguru import logger

# 项目根目录（backend/）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 用量数据库路径（与 deployments.db / audit.db 同目录）
_USAGE_DB = _PROJECT_ROOT / "checkpoints" / "usage.db"

# sqlite3.execute 一次只支持单条语句，建表与建索引必须分开执行
_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT,
    agent TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""
_SCHEMA_INDEX_THREAD = """
CREATE INDEX IF NOT EXISTS idx_usage_thread
    ON llm_usage(thread_id, created_at)
"""

# 每请求 thread_id 通道：api.py 在解析出 thread_id 后写入，
# UsageCallback 读取归属（模型实例跨会话共享，调用参数里没有 thread）。
_usage_thread_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "usage_thread_id", default=None
)


def set_usage_thread(thread_id: str | None) -> None:
    """写入当前请求的 thread_id（请求任务上下文内有效，不跨请求泄漏）。"""
    _usage_thread_ctx.set(thread_id)


def _now() -> str:
    """当前时间的 ISO 字符串（秒级精度，落盘用）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _extract_usage(response: LLMResult) -> tuple[int, int, int]:
    """从调用结果取 (input, output, total)，缺失时按 0 处理。

    取数顺序：生成消息的 usage_metadata（流式下用量合并进末 chunk 的消息，
    on_llm_end 的 llm_output 此时没有 token_usage）→ llm_output.token_usage。
    """
    generations = response.generations or []
    message = None
    if generations and generations[0]:
        message = getattr(generations[0][0], "message", None)
    meta = getattr(message, "usage_metadata", None) or {}
    if meta.get("input_tokens") or meta.get("output_tokens"):
        total = meta.get("total_tokens") or (
            (meta.get("input_tokens") or 0) + (meta.get("output_tokens") or 0)
        )
        return (
            int(meta.get("input_tokens") or 0),
            int(meta.get("output_tokens") or 0),
            int(total),
        )
    output = response.llm_output or {}
    usage = output.get("token_usage") or {}
    total = usage.get("total_tokens") or (
        (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
    )
    return (
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        int(total),
    )


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """把查询结果行转换为 dict。"""
    keys = [
        "id",
        "thread_id",
        "agent",
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "created_at",
    ]
    return {key: row[idx] for idx, key in enumerate(keys)}


class UsageStore:
    """token 用量 SQLite 存储（单连接，与 AuditLogStore 同模式）。"""

    def __init__(self, db_path: Path | str):
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def _get_conn(self) -> aiosqlite.Connection:
        """惰性连接：目录不存在自动创建，首次连接时建表。"""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self._db_path))
            await conn.execute(_SCHEMA_TABLE)
            await conn.execute(_SCHEMA_INDEX_THREAD)
            await conn.commit()
            self._conn = conn
        return self._conn

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def record(
        self,
        *,
        thread_id: str | None,
        agent: str,
        model: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """追加一条用量记录。字段校验失败时记录日志但不阻断调用。"""
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO llm_usage "
            "(thread_id, agent, model, input_tokens, output_tokens, "
            "total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                agent,
                model,
                max(0, int(input_tokens or 0)),
                max(0, int(output_tokens or 0)),
                max(0, int(total_tokens or 0)),
                _now(),
            ),
        )
        await conn.commit()
        logger.debug(
            "USAGE | event=recorded | thread_id={} | agent={} | total={}",
            thread_id,
            agent,
            total_tokens,
        )

    async def list(
        self,
        *,
        thread_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """查询用量记录（按时间倒序）。

        Args:
            thread_id: 非空时按 thread 过滤
            limit: 返回条数上限（1~200，超出按 200 处理）
        """
        conn = await self._get_conn()
        safe_limit = max(1, min(int(limit), 200))
        where = "WHERE thread_id = ?" if thread_id else ""
        values: list[Any] = [thread_id] if thread_id else []
        values.append(safe_limit)
        rows = await conn.execute_fetchall(
            f"SELECT * FROM llm_usage {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            values,
        )
        return [_row_to_dict(row) for row in rows]

    async def totals(self, thread_id: str) -> dict[str, int]:
        """汇总指定 thread 的用量与调用次数。"""
        conn = await self._get_conn()
        row = await conn.execute_fetchall(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(total_tokens), 0) "
            "FROM llm_usage WHERE thread_id = ?",
            (thread_id,),
        )
        calls, input_tokens, output_tokens, total_tokens = row[0]
        return {
            "calls": int(calls),
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "total_tokens": int(total_tokens),
        }


class UsageCallback(BaseCallbackHandler):
    """模型调用用量记录（挂模型实例，主链子链全捕获）。

    专员名构造时绑定；thread_id 从每请求 ContextVar 读（模型实例跨会话共享）。
    无法归属（ContextVar 为空，如测试直调）时跳过，不写孤儿行。
    """

    def __init__(self, agent: str, model: str | None = None, store: UsageStore | None = None):
        super().__init__()
        self.agent = agent
        self.model = model
        self._store = store

    def _get_store(self) -> UsageStore:
        if self._store is None:
            self._store = get_usage_store()
        return self._store

    # 注意：必须覆盖同步名的 async 版本（async def on_llm_end），而不是
    # aon_llm_end——dispatch 只 getattr(handler, "on_llm_end")，后者永不被调用。
    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """模型调用结束：记一行用量。异常只记日志，不阻断主流程。"""
        try:
            thread_id = _usage_thread_ctx.get()
            if thread_id is None:
                logger.debug("USAGE | event=skipped_no_thread | agent={}", self.agent)
                return
            input_tokens, output_tokens, total_tokens = _extract_usage(response)
            output = response.llm_output or {}
            model = output.get("model_name") or self.model
            await self._get_store().record(
                thread_id=thread_id,
                agent=self.agent,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        except Exception as exc:
            logger.warning("USAGE | event=record_failed | error={}", str(exc))


@lru_cache(maxsize=1)
def get_usage_store() -> UsageStore:
    """单例获取用量存储（默认 backend/checkpoints/usage.db）。

    测试场景直接构造 UsageStore(tmp_path / "x.db")，不走单例。
    """
    return UsageStore(_USAGE_DB)


async def close_usage_store() -> None:
    """关闭单例用量存储的连接（api.py lifespan 退出时调用）。"""
    store = get_usage_store()
    try:
        await store.close()
    except Exception as exc:
        logger.warning("USAGE | event=store_close_failed | error={}", str(exc))


__all__ = [
    "UsageStore",
    "UsageCallback",
    "get_usage_store",
    "close_usage_store",
    "set_usage_thread",
]
