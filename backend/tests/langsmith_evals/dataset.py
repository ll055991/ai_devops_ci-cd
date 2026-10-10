"""LangSmith 客户端、数据集上传、实验运行。

自包含：不依赖 backend/tests/evals 下任何文件。
"""

from __future__ import annotations

import os
from typing import Any

from langsmith import aevaluate
from loguru import logger

from .evaluators import gate_assertions, gate_pass
from .harness import target
from .scenarios import TEST_CASES

log = logger.info

DEFAULT_DATASET_NAME = "deploy-agent-gate"


def get_client() -> Any:
    """懒建 LangSmith 客户端（导入本模块不应强制要求凭证/联网）。"""
    from langsmith import Client

    return Client(api_key=os.getenv("LANGSMITH_API_KEY"))


def create_test_dataset(
    dataset_name: str = DEFAULT_DATASET_NAME,
    test_cases: list[dict[str, Any]] | None = None,
    *,
    recreate: bool = False,
) -> Any:
    """上传 golden 场景为 LangSmith 数据集（幂等）。

    幂等策略：存在即跳过，除非 recreate=True 先删旧再建——避免重复执行
    时把同一批样例灌进同一数据集。
    """
    cases = TEST_CASES if test_cases is None else test_cases
    client = get_client()

    try:
        existing = client.read_dataset(dataset_name=dataset_name)
    except Exception:
        existing = None

    if existing is not None:
        if not recreate:
            log(f"数据集已存在，跳过上传：{dataset_name}（重建请加 recreate=True）")
            return existing
        client.delete_dataset(dataset_id=existing.id)
        log(f"已删除旧数据集：{dataset_name}")

    dataset = client.create_dataset(
        dataset_name=dataset_name,
        description="M1 golden 门禁场景（本模块自包含复刻，与 tests/evals 独立）",
    )
    client.create_examples(dataset_id=dataset.id, examples=cases)
    log(f"数据集创建成功：{dataset_name} | examples={len(cases)}")
    return dataset


async def run_evaluation(
    dataset_name: str = DEFAULT_DATASET_NAME,
    *,
    experiment_prefix: str = "m1-gate",
    upload_results: bool = True,
    limit: int = 0,
) -> Any:
    """云端实验：数据集 × target × 评估器。

    max_concurrency=1 是硬约束：target 的隔离靠模块级全局替换，并发会让
    A 场景的 FakeSSH/审批 mock/临时库被 B 场景覆盖，产生串台假结果。
    """
    client = get_client()
    data: Any = dataset_name
    if limit > 0:
        # 冒烟：只取前 N 条（避免首跑就把 10 条慢场景全跑一遍）
        data = list(client.list_examples(dataset_name=dataset_name, limit=limit))

    results = await aevaluate(
        target,
        data=data,
        evaluators=[gate_assertions, gate_pass],
        max_concurrency=1,  # ★ 禁止调大，原因见 docstring
        experiment_prefix=experiment_prefix,
        upload_results=upload_results,
        metadata={"suite": "m1-gate", "self_contained": True},
    )
    log(f"实验完成：{experiment_prefix} | url={getattr(results, 'url', None)}")
    return results