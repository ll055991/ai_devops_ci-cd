"""用量账本单元测试（M2）。

覆盖三个离线 seam（无 LLM 凭证、无网络）：
- 存储缝：临时库 record / list 过滤 / totals 求和
- callback 缝：伪造调用结果进 aon_llm_end → 落库行正确；无归属跳过；写失败不抛
- 路由缝：GET /api/agent/usage 返回明细 + 总量（应用内传输 + 临时库替换单例）
- 出厂 helper：build_model_for_agent 返回带专员 callback 的模型；
  子Agent 规格各持独立模型实例（专员维度不断链）

测试策略：伪造 LLMResult，不调真实模型；临时库隔离，不碰单例。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.outputs import Generation, LLMResult

from deploy_agent import api
from deploy_agent.factory import build_model_for_agent, build_subagents
from deploy_agent.settings import Settings
from deploy_agent.tools import build_tools
from deploy_agent.usage import (
    UsageCallback,
    UsageStore,
    _extract_usage,
    set_usage_thread,
)


def _make_settings() -> Settings:
    return Settings(
        container_names_raw="ontology-graph",
        workspaces_raw="/data/deploy/workspace",
        image_prefixes_raw="ontology/ontology-graph",
        whitelist_file="__nonexistent_whitelist_test__.json",
        server_host="10.1.248.143",
        server_port=22,
        server_user="root",
        server_password="secret",
        gitlab_user="user",
        gitlab_token="token",
        health_url="http://127.0.0.1:8080/healthz",
        openai_api_key="sk-test",
    )


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


@pytest.fixture
async def store(tmp_path):
    s = UsageStore(tmp_path / "usage.db")
    yield s
    await s.close()


@pytest.fixture
def thread():
    """每测试独立的用量 thread 归属，用完清零。"""
    set_usage_thread("t-usage")
    yield "t-usage"
    set_usage_thread(None)


def _mock_transport(prompt=10, completion=20):
    """ httpx MockTransport：返回带用量的 canned chat completion。不联网。"""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "created": 1,
                "model": "mock",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
            },
        )

    return httpx.MockTransport(handler)


def _bound_model(agent: str, store) -> Any:
    """带用量 callback 的绑定模型：走真实 dispatch（bind → ainvoke → on_llm_end）。

    直接调 cb.aon_llm_end 是错误 seam——框架 dispatch 只找 on_llm_end。
    """
    from langchain_openai import ChatOpenAI

    client = httpx.AsyncClient(transport=_mock_transport(), base_url="http://test/v1")
    model = ChatOpenAI(model="mock", api_key="sk-test", http_async_client=client)
    model.callbacks = [UsageCallback(agent=agent, model="mock", store=store)]
    return model.bind_tools([])


# ==================== 存储缝 ====================


async def test_store_record_and_list(store):
    """record 写入后 list 可查，字段齐全。"""
    await store.record(
        thread_id="t-1", agent="main", model="m",
        input_tokens=10, output_tokens=20, total_tokens=30,
    )
    rows = await store.list(thread_id="t-1")
    assert len(rows) == 1
    row = rows[0]
    assert (row["agent"], row["model"]) == ("main", "m")
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (10, 20, 30)
    assert row["created_at"]


async def test_store_totals(store):
    """totals 按 thread 求和调用次数与三类 token。"""
    await store.record(thread_id="t-1", agent="main", model="m",
                       input_tokens=10, output_tokens=20, total_tokens=30)
    await store.record(thread_id="t-1", agent="code-agent", model="m",
                       input_tokens=5, output_tokens=5, total_tokens=10)
    await store.record(thread_id="t-2", agent="main", model="m",
                       input_tokens=100, output_tokens=100, total_tokens=200)
    totals = await store.totals("t-1")
    assert totals == {"calls": 2, "input_tokens": 15, "output_tokens": 25, "total_tokens": 40}


# ==================== callback 缝 ====================


async def test_callback_records_row(store, thread):
    """走真实 dispatch：模型调用结束 → 落库一行，专员/thread/用量正确。"""
    await _bound_model("code-agent", store).ainvoke("ping")

    rows = await store.list(thread_id="t-usage")
    assert len(rows) == 1
    assert rows[0]["agent"] == "code-agent"
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"], rows[0]["total_tokens"]) == (10, 20, 30)


async def test_callback_skips_without_thread(store):
    """无归属（ContextVar 为空）时跳过，不写孤儿行。"""
    set_usage_thread(None)
    await _bound_model("main", store).ainvoke("ping")

    assert await store.list() == []


def test_extract_usage_prefers_message_metadata():
    """流式形状：用量在生成消息的 usage_metadata 里，llm_output 为空。"""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration

    result = LLMResult(
        generations=[[
            ChatGeneration(
                message=AIMessage(
                    content="hi",
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150,
                    },
                )
            )
        ]],
        llm_output={},
    )
    assert _extract_usage(result) == (100, 50, 150)


def test_extract_usage_falls_back_to_llm_output():
    """非流式形状：消息无用量，回退 llm_output.token_usage。"""
    result = LLMResult(
        generations=[[Generation(text="hi")]],
        llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": 8}},
    )
    assert _extract_usage(result) == (7, 8, 15)


async def test_callback_store_failure_does_not_raise(store, thread, monkeypatch):
    """写库失败只记日志不抛（可观测不拖累主流程）。"""
    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "record", boom)
    await _bound_model("main", store).ainvoke("ping")  # 不抛即通过


# ==================== 出厂装配 ====================


def test_build_model_for_agent_binds_callback(settings):
    """helper 返回的模型带专员用量 callback。"""
    model = build_model_for_agent(settings, "build-agent")
    agents = [getattr(cb, "agent", None) for cb in (model.callbacks or [])]
    assert "build-agent" in agents


def test_subagents_carry_own_models(settings):
    """子Agent 规格各持独立模型实例（专员维度不断链），不用回退共享实例。"""
    specs = build_subagents(build_tools(settings), settings)
    assert len(specs) == 4
    for spec in specs:
        model = spec.get("model")
        assert model is not None, f"{spec['name']} 没有独立模型"
        agents = [getattr(cb, "agent", None) for cb in (model.callbacks or [])]
        assert spec["name"] in agents, f"{spec['name']} 的模型缺专员 callback"


# ==================== 路由缝 ====================


async def test_list_usage_route(monkeypatch, store):
    """GET /api/agent/usage 返回明细 + 总量。"""
    await store.record(thread_id="t-1", agent="main", model="m",
                       input_tokens=10, output_tokens=20, total_tokens=30)
    await store.record(thread_id="t-1", agent="monitor-agent", model="m",
                       input_tokens=5, output_tokens=5, total_tokens=10)
    monkeypatch.setattr(api, "get_usage_store", lambda: store)

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agent/usage", params={"thread_id": "t-1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["count"] == 2
    assert body["totals"] == {
        "calls": 2, "input_tokens": 15, "output_tokens": 25, "total_tokens": 40,
    }
    assert {r["agent"] for r in body["records"]} == {"main", "monitor-agent"}
