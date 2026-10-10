"""LangSmith 数据集与自动化评估（入口 / CLI）。

模块划分（全部自包含，不依赖 backend/tests/evals）：
    scenarios.py   场景定义与 dataset example 构造
    harness.py     隔离替身（FakeSSH/临时库）+ target function
    assertions.py  确定性断言库（M1 5 指标）
    evaluators.py  LangSmith code evaluator 封装
    dataset.py     客户端、数据集上传、云端实验

运行（backend/ 目录下，中文路径勿用 uv run）：
    离线自检（不联网、不上传，仅需内网 LLM）：
        .venv\\Scripts\\python -m tests.langsmith_evals.langsmith_evals --local
    上传 golden 场景为数据集（需 LangSmith 连通）：
        .venv\\Scripts\\python -m tests.langsmith_evals.langsmith_evals --upload
    跑云端实验（需先 --upload）：
        .venv\\Scripts\\python -m tests.langsmith_evals.langsmith_evals --evaluate
"""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

from loguru import logger

from .dataset import DEFAULT_DATASET_NAME, create_test_dataset, run_evaluation
from .evaluators import gate_assertions
from .harness import target
from .scenarios import GATE_SCENARIOS, scenario_to_example

log = logger.info


async def run_local_check() -> list[tuple[str, list[tuple[str, bool, str]]]]:
    """离线自检：不联网、不上传，仅跑 target + 评估器，验证对接链路可用。

    用 SimpleNamespace 伪造 run/example 形态（均有 .outputs 属性），
    等价于本地 evaluate() 传入的 RunTree，从而在不依赖 LangSmith 云端
    的前提下验证 target 与 evaluator 两个新组件。
    """
    report: list[tuple[str, list[tuple[str, bool, str]]]] = []
    for scenario in GATE_SCENARIOS:
        example = scenario_to_example(scenario)
        outputs = await target(example["inputs"])
        run = SimpleNamespace(outputs=outputs)
        fake_example = SimpleNamespace(outputs=example["outputs"])
        rows = gate_assertions(run, fake_example)["results"]
        report.append(
            (scenario.id, [(r["key"], bool(r["score"]), r["comment"]) for r in rows])
        )
    return report


def _print_report(report: list[tuple[str, list[tuple[str, bool, str]]]]) -> None:
    total = sum(len(rows) for _, rows in report)
    failed = sum(1 for _, rows in report for _, ok, _ in rows if not ok)
    log(f"离线自检完成：场景={len(report)} | 断言={total} | 失败={failed}")
    for scenario_id, rows in report:
        for name, ok, detail in rows:
            log(f"{scenario_id} | {name} | {'OK' if ok else 'FAIL'} | {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LangSmith 数据集构建与自动化评估")
    parser.add_argument("--dataset", default=DEFAULT_DATASET_NAME, help="数据集名")
    parser.add_argument("--upload", action="store_true", help="上传 golden 场景到数据集")
    parser.add_argument("--recreate", action="store_true", help="删旧数据集后重建")
    parser.add_argument("--evaluate", action="store_true", help="跑云端实验")
    parser.add_argument("--local", action="store_true", help="离线自检（不联网）")
    parser.add_argument(
        "--no-upload-results",
        action="store_true",
        help="实验本地跑但不上传结果（配合 --evaluate）",
    )
    parser.add_argument("--limit", type=int, default=0, help="实验只跑前 N 条（0=全部）")
    args = parser.parse_args()

    if args.upload:
        create_test_dataset(args.dataset, recreate=args.recreate)
    if args.evaluate:
        asyncio.run(
            run_evaluation(
                args.dataset,
                upload_results=not args.no_upload_results,
                limit=args.limit,
            )
        )
    # 无参数或显式 --local 时做离线自检
    if args.local or not (args.upload or args.evaluate):
        _print_report(asyncio.run(run_local_check()))


if __name__ == "__main__":
    main()