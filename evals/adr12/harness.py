"""Running the comparison and recording what it measured (FR-62).

Every task runs on every arm, `repetitions` times. Each run gets its own copy of the fixture
folder under `workdir`, which lies outside the repository, so one run cannot see another's
files and no arm can reach the repository itself.

Each row carries what FR-62 names: whether the checker passed, the terminal status, turns, the
token counts, wall time, and the cost computed from one ModelPricing for both arms. The Claude
Agent SDK's own cost estimate is recorded separately, labelled as its estimate. A measure that
an arm does not report is the string "unavailable", never 0 (AC-50).
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from agentsdk.registry import call_cost

from . import report as report_module
from .tasks import FIXTURE_ROOT

UNAVAILABLE = "unavailable"


@dataclass
class Comparison:
    """What one comparison measured, and where it was written."""

    rows: list[dict[str, Any]]
    metadata: dict[str, Any]
    json_path: pathlib.Path
    markdown_path: pathlib.Path


def run_comparison(
    *,
    arms: list[Any],
    tasks: Any,
    repetitions: int,
    pricing: Any,
    reports_dir: pathlib.Path,
    workdir: pathlib.Path,
    gateway_model: str,
    stamp: str | None = None,
) -> Comparison:
    """Run every task on every arm, write the report, and return the rows."""
    reports_dir, workdir = pathlib.Path(reports_dir), pathlib.Path(workdir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for repetition in range(1, repetitions + 1):
        for task in tasks:
            for arm in arms:
                rows.append(_one_run(arm, task, repetition, pricing, workdir))
    metadata = {
        "date": (stamp or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        "gateway_model": gateway_model,
        "arms": [arm.name for arm in arms],
        "versions": {arm.name: arm.version for arm in arms},
        "wire_formats": {arm.name: arm.wire_format for arm in arms},
        "repetitions": repetitions,
        "tasks": [task.id for task in tasks],
        "pricing": pricing.to_json() if hasattr(pricing, "to_json") else str(pricing),
    }
    json_path, markdown_path = report_module.write_report(metadata, rows, reports_dir)
    return Comparison(rows=rows, metadata=metadata, json_path=json_path, markdown_path=markdown_path)


def _one_run(arm: Any, task: Any, repetition: int, pricing: Any, workdir: pathlib.Path) -> dict[str, Any]:
    root = workdir / f"{task.id}-{arm.name}-{repetition}"
    if root.exists():  # pragma: no cover - a repeated stamp would hide a run
        shutil.rmtree(root)
    shutil.copytree(FIXTURE_ROOT, root)
    outcome = asyncio.run(arm.run(task, root))
    usage = outcome.usage
    cost = call_cost(usage, pricing)
    return {
        "task": task.id,
        "kind": task.kind,
        "arm": arm.name,
        "repetition": repetition,
        # The checker decides from the answer alone, and always answers True or False.
        "passed": bool(task.checker(outcome.answer)),
        "status": outcome.status,
        "turns": outcome.turns,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "wall_ms": round(outcome.wall_ms, 1),
        "cost_usd": _money(cost),
        "sdk_cost_estimate_usd": _money(outcome.sdk_cost_estimate),
        "run_root": str(root),
        "answer": _short(outcome.answer),
        "error": outcome.error or UNAVAILABLE,
    }


def _money(value: Decimal | None) -> str:
    """A cost as text, or unavailable: an unknown cost is never 0 (NFR-11, AC-50)."""
    return UNAVAILABLE if value is None else str(value)


def _short(answer: str | None) -> str:
    """The answer, trimmed: enough to see what an arm said, short enough to read in a table."""
    if not isinstance(answer, str) or not answer.strip():
        return UNAVAILABLE
    text = " ".join(answer.split())
    return text if len(text) <= 160 else text[:157] + "..."
