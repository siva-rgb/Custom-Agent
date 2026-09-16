"""Writing the comparison's JSON and Markdown (FR-62).

The report names the model, the gateway model id, both SDK versions, each arm's wire format and
the date, and reports every gap as measured. It is an input to the second increment's
specification and to Phase 5A, so it states what happened and leaves the conclusions to the
reader: no arm is declared better here.
"""

from __future__ import annotations

import json
import pathlib
import statistics
from typing import Any

UNAVAILABLE = "unavailable"


def write_report(metadata: dict[str, Any], rows: list[dict[str, Any]], reports_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Write both files and return their paths."""
    reports_dir = pathlib.Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = "adr12-comparison-" + str(metadata.get("date", ""))[:10]
    json_path = reports_dir / f"{stem}.json"
    markdown_path = reports_dir / f"{stem}.md"
    json_path.write_text(json.dumps({"metadata": metadata, "rows": rows}, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(build_markdown(metadata, rows), encoding="utf-8")
    return json_path, markdown_path


def build_markdown(metadata: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    arms = list(metadata.get("arms", []))
    lines = [
        "# ADR-12 comparison",
        "",
        f"- Date: {metadata.get('date')}",
        f"- Model, as the gateway names it: {metadata.get('gateway_model')}",
        f"- Repetitions per task and arm: {metadata.get('repetitions')}",
        f"- Tasks: {len(metadata.get('tasks', []))}",
        "",
        "## Arms",
        "",
        "| arm | version | wire format |",
        "|---|---|---|",
    ]
    for arm in arms:
        version = metadata.get("versions", {}).get(arm, UNAVAILABLE)
        wire = metadata.get("wire_formats", {}).get(arm, UNAVAILABLE)
        lines.append(f"| {arm} | {version} | {wire} |")
    lines += [
        "",
        "Both arms ran the same tasks, with the same model through the same gateway, the same turn",
        "limit and read-only tools over their own copy of the fixture folder. Both are costed from",
        "one price list; the Claude Agent SDK's own figure is its estimate and is reported as such.",
        "",
        "## What each arm measured",
        "",
        "| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        taken = [row for row in rows if row["arm"] == arm]
        lines.append(
            f"| {arm} | {sum(1 for row in taken if row['passed'])} of {len(taken)} | {len(taken)} | "
            f"{_median(taken, 'turns')} | {_median(taken, 'wall_ms')} | {_total(taken, 'cost_usd')} | "
            f"{_total(taken, 'sdk_cost_estimate_usd')} |"
        )
    lines += ["", "## By task", "", "| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['task']} | {row['kind']} | {row['arm']} | {'yes' if row['passed'] else 'no'} | {row['status']} | "
            f"{row['turns']} | {row['wall_ms']} | {row['cost_usd']} | {row['sdk_cost_estimate_usd']} | "
            f"{_cell(row['answer'])} |"
        )
    lines += [
        "",
        "## How to read this",
        "",
        "Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a",
        "gap in the data cannot be mistaken for a zero. A failed checker means the answer did not",
        "match what the fixture says, which is a measurement of this task on this arm, not a",
        "judgement of either SDK.",
        "",
    ]
    return "\n".join(lines)


def _median(rows: list[dict[str, Any]], key: str) -> str:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float)) and not isinstance(row[key], bool)]
    return UNAVAILABLE if not values else str(round(statistics.median(values), 1))


def _total(rows: list[dict[str, Any]], key: str) -> str:
    from decimal import Decimal

    total, seen = Decimal(0), False
    for row in rows:
        value = row.get(key)
        if isinstance(value, str) and value != UNAVAILABLE:
            try:
                total += Decimal(value)
                seen = True
            except Exception:  # noqa: BLE001 - a value that is not money is not counted
                continue
    return str(total) if seen else UNAVAILABLE


def _cell(text: Any) -> str:
    """A table cell: one line, with the separator escaped."""
    return " ".join(str(text).split()).replace("|", "\\|")
