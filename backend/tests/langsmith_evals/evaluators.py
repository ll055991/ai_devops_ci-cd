"""LangSmith code evaluator 封装。

自包含：不依赖 backend/tests/evals 下任何文件。
把 assertions 的确定性断言包装成 LangSmith evaluator：签名 (run, example)，
返回 {"results": [...]} 或 {"key","score","comment"}。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .assertions import evaluate
from .harness import Trajectory, eval_settings

# 断言只读白名单值，whitelist_file 指向临时路径即可，避免误读生产
# backend/whitelist.json 而污染判定
_SETTINGS = eval_settings(Path(tempfile.gettempdir()) / "langsmith-eval-whitelist.json")


def _unpack(run: Any, example: Any) -> tuple[Trajectory, dict[str, Any], str]:
    """取 (轨迹, 参考答案, scenario_id)。

    兼容两种 run 形态：本地 evaluate() 传 RunTree（属性访问），
    上传沙箱/测试面板传 dict（下标访问）。
    """
    outputs = run.outputs if hasattr(run, "outputs") else ((run or {}).get("outputs") or {})
    reference = (
        example.outputs
        if hasattr(example, "outputs")
        else ((example or {}).get("outputs") or {})
    )
    return Trajectory(**outputs["trajectory"]), reference, outputs.get("scenario_id", "")


def run_assertions(run: Any, example: Any) -> list[tuple[str, bool, str]]:
    """按参考答案跑断言库，返回 [(指标, 通过, 说明)]。"""
    traj, reference, sid = _unpack(run, example)
    return evaluate(
        traj,
        _SETTINGS,
        reference.get("expect") or {},
        f"eval-{sid}",
        reference.get("required_tools") or [],
    )


def gate_assertions(run: Any, example: Any) -> dict[str, Any]:
    """门禁全量断言：一次返回多个指标（与 baseline.md 同粒度）。

    SDK 的 EvaluationResults 支持一次返回多指标（{"results": [{"key","score","comment"}]}），
    故不拆成多个 evaluator，指标名与本地基线报告完全一致，便于新旧对照。
    """
    results = run_assertions(run, example)
    return {
        "results": [
            {"key": name, "score": 1 if passed else 0, "comment": detail}
            for name, passed, detail in results
        ]
    }


def gate_pass(run: Any, example: Any) -> dict[str, Any]:
    """门禁总分：该场景全部断言通过=1（回归对比看这一列即可）。"""
    results = run_assertions(run, example)
    passed = all(p for _, p, _ in results)
    detail = "; ".join(
        f"{name}:{'OK' if ok else 'FAIL ' + text}" for name, ok, text in results
    )
    return {"key": "gate_pass", "score": 1 if passed else 0, "comment": detail}