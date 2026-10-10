"""隔离替身 + target function（LangSmith 实验的被测目标）。

自包含：不依赖 backend/tests/evals 下任何文件。
唯一 seam：真调小模型 + SSH 命令层 mock + 审批中断 mock；断言只看审计记录 +
工具调用序列 + 部署状态，不新增低层 seam。

为什么能在 LangSmith 的 target 里复现隔离：evaluate() 的 target 与评估器
**同进程执行**，pytest 的 monkeypatch 本质只是"模块属性替换"，这里换成
"手动替换 + finally 还原"即可，无需真 SSH / 真审批 / 真生产库。
"""

from __future__ import annotations

import asyncio
import ast
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from deploy_agent import middleware as middleware_module
from deploy_agent import tools as tools_module
from deploy_agent import usage as usage_module
from deploy_agent.audit import AuditLogStore
from deploy_agent.factory import create_deploy_agent
from deploy_agent.settings import Settings
from deploy_agent.state import DeploymentStateStore
from deploy_agent.usage import UsageStore, set_usage_thread

from .scenarios import DEFAULT_ROUTES, Scenario, scenario_from_inputs

APPROVAL_TOOLS_RAW = (
    "stop_container,remove_container,start_container,"
    "write_workspace_file,delete_workspace_file,"
    "add_whitelist_entry,remove_whitelist_entry,rollback_deployment"
)


@dataclass
class Trajectory:
    """单场景轨迹：只含外部可观察行为，不断言 LLM 原文。

    calls 是原子工具调用列表（含子 Agent 内的调用），来源为审计记录——
    主图 messages 只能看到 task/主 Agent 工具，子 Agent 的 git/build/巡检
    调用在嵌套图里，必须从审计还原。
    """

    scenario_id: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    deployment: dict[str, Any] | None = None
    final_text: str = ""
    ssh_calls: list[str] = field(default_factory=list)


class FakeSSH:
    """按命令关键字路由的 SSH 替身。

    ssh_raise 命中的命令抛 ConnectionError；routes 优先于默认路由；
    未命中返回成功空输出。stream 版本逐行回调 on_line。
    """

    def __init__(self, scenario: Scenario):
        self.routes = list(scenario.ssh_routes) + DEFAULT_ROUTES
        self.raise_on = list(scenario.ssh_raise)
        self.calls: list[str] = []

    def _match(self, command: str) -> tuple[int, str, str]:
        if any(k in command for k in self.raise_on):
            raise ConnectionError("SSH connection refused (eval fake)")
        for keyword, response in self.routes:
            if keyword in command:
                return response
        return 0, "", ""

    async def run_ssh(self, settings: Any, command: str, timeout: int = 60):
        self.calls.append(command)
        return self._match(command)

    async def run_ssh_stream(
        self, settings: Any, command: str, timeout: int = 600, on_line: Any = None
    ):
        self.calls.append(command)
        exit_code, out, err = self._match(command)
        if on_line is not None:
            for line in out.splitlines():
                on_line(line)
        return exit_code, out, err


def eval_settings(workdir: Path) -> Settings:
    """测试用 Settings：全字段显式注入，whitelist_file 指向临时路径隔离生产 JSON。"""
    return Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace,/data/test",
        image_prefixes_raw="ontology/ontology-graph,infra/data-service",
        whitelist_file=str(workdir / "whitelist.json"),
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
        approval_required_tools_raw=APPROVAL_TOOLS_RAW,
    )


def _parse_args_summary(raw: Any) -> dict[str, Any]:
    """审计行 args_summary（打码参数 repr）解析回 dict；失败返回空 dict。"""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _all_called(rows: list[dict[str, Any]], required: list[str]) -> bool:
    """审计里是否已出现全部必调工具（用于逐轮推进/提前结束）。"""
    if not required:
        return False
    called = {r.get("tool_name") for r in rows}
    return all(name in called for name in required)


def calls_from_audit(rows_desc: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """审计行（created_at DESC, id DESC）→ 原子调用列表（按发生顺序）。"""
    calls: list[dict[str, Any]] = []
    for row in reversed(rows_desc):
        calls.append(
            {
                "name": row.get("tool_name", ""),
                "status": row.get("status", ""),
                "args": _parse_args_summary(row.get("args_summary")),
                "thread_id": row.get("thread_id"),
                "result_summary": row.get("result_summary"),
            }
        )
    return calls


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


@contextmanager
def patched_eval_env(scenario: Scenario, workdir: Path) -> Iterator[dict[str, Any]]:
    """进程内复现 pytest 版隔离：手动替换模块属性，退出时还原。

    7 处替换对应"4 个 store 单例 + 2 个 SSH 入口 + 审批中断"。只 patch 一处不够：
    工具与中间件各自 import 了 get_deployment_store，漏 patch 会让
    rollback/get_deployment_status 读到生产库。
    这些替换是**模块级全局生效**的，故实验必须串行（见 dataset.run_evaluation）。
    """
    dep_store = DeploymentStateStore(workdir / "deployments.db")
    audit_store = AuditLogStore(workdir / "audit.db")
    usage_store = UsageStore(workdir / "usage.db")
    fake = FakeSSH(scenario)
    approvals: list[dict[str, Any]] = []

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        approvals.append(payload)
        return {"decisions": scenario.decisions}

    patches: dict[tuple[Any, str], Any] = {
        (middleware_module, "get_deployment_store"): lambda: dep_store,
        (middleware_module, "get_audit_store"): lambda: audit_store,
        (tools_module, "get_deployment_store"): lambda: dep_store,
        (usage_module, "get_usage_store"): lambda: usage_store,
        (tools_module, "_run_ssh"): fake.run_ssh,
        (tools_module, "_run_ssh_stream"): fake.run_ssh_stream,
        (middleware_module, "interrupt"): fake_interrupt,
    }
    saved = {key: getattr(key[0], key[1]) for key in patches}
    for (module, attr), value in patches.items():
        setattr(module, attr, value)
    try:
        yield {
            "dep": dep_store,
            "audit": audit_store,
            "usage": usage_store,
            "approvals": approvals,
            "fake": fake,
            "settings": eval_settings(workdir),
        }
    finally:
        for (module, attr), value in saved.items():
            setattr(module, attr, value)


async def run_scenario_standalone(scenario: Scenario, timeout: int = 300) -> dict[str, Any]:
    """非 pytest 环境跑一条场景，返回 evaluator 所需的 outputs 结构。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patched_eval_env(scenario, Path(tmp)) as env:
            settings: Settings = env["settings"]
            # 与 pytest 版不同：无凭证时抛错而非 skip，让实验如实记录该例失败
            if settings.openai_api_key is None:
                raise RuntimeError("评估需要 OPENAI_API_KEY（内网端点）")

            seed_id = ""
            if scenario.seed:
                rec = await env["dep"].record(scenario.thread_id, **scenario.seed)
                seed_id = str(rec.get("id", ""))

            set_usage_thread(scenario.thread_id)
            agent = create_deploy_agent(settings, checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": scenario.thread_id}}
            message = scenario.message.format(deployment_id=seed_id)
            traj = Trajectory(
                scenario_id=scenario.id,
                approvals=env["approvals"],
                ssh_calls=env["fake"].calls,
            )
            try:
                # 首轮 + 追问轮：必调工具没跑齐就继续追问，跑齐即提前结束——
                # 与本地门禁同策略，保证云端 target 的轮次行为可复现
                for user_text in [message, *scenario.follow_ups]:
                    result = await asyncio.wait_for(
                        agent.ainvoke(
                            {"messages": [{"role": "user", "content": user_text}]}, config
                        ),
                        timeout,
                    )
                    messages = result.get("messages", []) if isinstance(result, dict) else []
                    for msg in messages:
                        if isinstance(msg, AIMessage):
                            text = extract_text(msg.content)
                            if text.strip():
                                traj.final_text = text
                    if scenario.required_tools:
                        rows = await env["audit"].list(limit=200)
                        if _all_called(rows, scenario.required_tools):
                            break

                traj.audit_rows = await env["audit"].list(limit=200)
                traj.calls = calls_from_audit(traj.audit_rows)
                traj.deployment = await env["dep"].get_by_thread(scenario.thread_id)
                return {
                    "scenario_id": scenario.id,
                    "final_text": traj.final_text,
                    "trajectory": asdict(traj),  # 展开 calls/approvals/audit_rows/...
                }
            finally:
                set_usage_thread(None)
                await env["dep"].close()
                await env["audit"].close()
                await env["usage"].close()


async def target(inputs: dict[str, Any]) -> dict[str, Any]:
    """LangSmith aevaluate 的 target：inputs 即 dataset example 的 inputs。"""
    return await run_scenario_standalone(scenario_from_inputs(inputs))