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
    """Write both files and return their paths. A report never replaces another: a second one on the
    same date is named -2, then -3 (M51: the final comparison overwrote M50's of the same day). Each
    file is created exclusively, so two writers at once never share a name (pre-review F5)."""
    reports_dir = pathlib.Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = "adr12-comparison-" + str(metadata.get("date", ""))[:10]
    texts = (json.dumps({"metadata": metadata, "rows": rows}, indent=2, sort_keys=True),
             build_markdown(metadata, rows))
    number = 1
    while True:
        stem = base if number == 1 else f"{base}-{number}"
        json_path, markdown_path = reports_dir / f"{stem}.json", reports_dir / f"{stem}.md"
        number += 1
        try:
            with open(json_path, "x", encoding="utf-8") as claimed:
                claimed.write(texts[0])
        except FileExistsError:
            continue
        try:
            with open(markdown_path, "x", encoding="utf-8") as claimed:
                claimed.write(texts[1])
        except FileExistsError:
            json_path.unlink()
            continue
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
        *([f"- **Incomplete:** {metadata['incomplete']}."] if metadata.get("incomplete") else []),
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
    setups = metadata.get("setups")
    if setups:
        # FR-179: the task count, each kind's setup, the budget rule, networking and the images.
        lines += ["", "## Tasks and setups", "",
                  f"{metadata.get('task_count')} tasks: "
                  + ", ".join(f"{count} {kind}" for kind, count in metadata.get("kinds", {}).items()) + ".",
                  "", "| kind | tasks | arm | setup | USD ceiling | token ceiling |", "|---|---|---|---|---|---|"]
        for kind, by_arm in setups.items():
            ceiling = metadata.get("ceilings", {}).get(kind, {})
            for arm in arms:
                budgeted = arm == "agentsdk"
                lines.append(f"| {kind} | {metadata.get('kinds', {}).get(kind)} | {arm} | {_cell(by_arm.get(arm, UNAVAILABLE))} | "
                             f"{ceiling.get('usd', UNAVAILABLE) if budgeted else 'none'} | "
                             f"{ceiling.get('tokens', UNAVAILABLE) if budgeted else 'none'} |")
        lines += ["", f"- Budget rule: {metadata.get('budget_rule')}."]
        if metadata.get("networking"):
            lines.append(f"- Networking: {metadata['networking']}.")
        if metadata.get("preset_version"):
            lines.append(f"- The general preset's version: {metadata['preset_version']}.")
        if metadata.get("verification"):
            lines.append(f"- Verification: {metadata['verification']}; a run's own check.txt is not evidence.")
        witness = metadata.get("witness")
        if witness:
            lines.append(f"- Fetch witness: {witness.get('url')} as the harness fetched it at {witness.get('fetched_at')}, "
                         f"{witness.get('bytes')} bytes, SHA-256 {witness.get('sha256')}; the fetch tasks are judged "
                         "against it.")
        if metadata.get("fetch_proof"):
            lines.append(f"- Proof of fetch: {metadata['fetch_proof']}.")
        if metadata.get("swept"):
            lines.append(f"- Containers removed by the closing sweep: {', '.join(metadata['swept'])}.")
        for arm, digest in (metadata.get("images") or {}).items():
            lines.append(f"- {arm} image: {digest}")
        lines += ["", "Both arms use the same model through the same gateway and each task's turn limit. Both",
                  "are costed from one price list; the Claude Agent SDK's own figure is its estimate and is",
                  "reported as such."]
    else:
        lines += [
            "",
            "Both arms ran the same tasks, with the same model through the same gateway, the same turn",
            "limit and read-only tools over their own copy of the fixture folder. Both are costed from",
            "one price list; the Claude Agent SDK's own figure is its estimate and is reported as such.",
        ]
    lines += [
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
    if setups:
        # FR-181: per kind and overall, each gap as measured.
        lines += ["", "## By kind", "",
                  "| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost "
                  "| median wall ms |", "|---|---|---|---|---|---|---|---|---|---|"]
        for kind in [*setups, ALL]:
            for arm in arms:
                taken = [row for row in rows if row["arm"] == arm and (kind == ALL or row["kind"] == kind)]
                lines.append(_kind_row(kind, arm, taken))
    lines += ["", "## By task", "", "| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['task']} | {row['kind']} | {row['arm']} | {'yes' if row['passed'] else 'no'} | {row['status']} | "
            f"{row['turns']} | {row['wall_ms']} | {row['cost_usd']} | {row['sdk_cost_estimate_usd']} | "
            f"{_cell(row['answer'])} |"
        )
    fetches = [row for row in rows if row.get("group") == "fetch"]
    if fetches:
        # Round-1 F1: what showed each fetch, so a credit can be checked from the report itself.
        lines += ["", "## Fetch evidence", "", "| task | arm | repetition | passed | hosts shown | evidence |",
                  "|---|---|---|---|---|---|"]
        for row in fetches:
            lines.append(f"| {row['task']} | {row['arm']} | {row.get('repetition')} | {'yes' if row['passed'] else 'no'} | "
                         f"{', '.join(row.get('fetched') or []) or 'none'} | "
                         f"{_cell('; '.join(row.get('fetch_evidence') or [])) or 'none'} |")
    delegations = [row for row in rows if row.get("kind") == "delegation"]
    if delegations:
        # FR-188: whether each run delegated and how many children it ran, never a condition of passing.
        lines += ["", "## Delegation", ""]
        if metadata.get("delegation"):
            lines += [f"How it is measured: {metadata['delegation']}.", ""]
        lines += ["| arm | runs | delegated | children in all |", "|---|---|---|---|"]
        for arm in arms:
            taken = [row for row in delegations if row["arm"] == arm]
            if taken:
                counted = [row["children"] for row in taken if isinstance(row.get("children"), int)]
                lines.append(f"| {arm} | {len(taken)} | {sum(1 for row in taken if row.get('delegated') is True)} | "
                             f"{sum(counted) if len(counted) == len(taken) else UNAVAILABLE} |")
        lines += ["", "| task | arm | repetition | passed | delegated | children |", "|---|---|---|---|---|---|"]
        for row in delegations:
            delegated = row.get("delegated", UNAVAILABLE)
            lines.append(f"| {row['task']} | {row['arm']} | {row.get('repetition')} | {'yes' if row['passed'] else 'no'} "
                         f"| {delegated if delegated == UNAVAILABLE else ('yes' if delegated else 'no')} "
                         f"| {row.get('children', UNAVAILABLE)} |")
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


# The by-kind table's overall row (FR-181).
ALL = "all"


def _kind_row(kind: str, arm: str, taken: list[dict[str, Any]]) -> str:
    statuses: dict[str, int] = {}
    for row in taken:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    passed = sum(1 for row in taken if row["passed"])
    rate = f"{100 * passed / len(taken):.0f}%" if taken else UNAVAILABLE
    return (f"| {kind} | {arm} | {passed} | {rate} | {len(taken)} | "
            f"{', '.join(f'{s} {n}' for s, n in sorted(statuses.items())) or 'none'} | "
            f"{_median(taken, 'turns')} | {_tokens(taken)} | {_total(taken, 'cost_usd')} | {_median(taken, 'wall_ms')} |")


def _tokens(rows: list[dict[str, Any]]) -> str:
    """Prompt and completion tokens summed; unavailable when any row's count is: a total over runs some
    of which were not counted is no total (M53 round 1 NB-1)."""
    counts = [row.get(key) for row in rows for key in ("prompt_tokens", "completion_tokens")]
    whole = counts and all(isinstance(c, int) and not isinstance(c, bool) for c in counts)
    return str(sum(counts)) if whole else UNAVAILABLE


def _median(rows: list[dict[str, Any]], key: str) -> str:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float)) and not isinstance(row[key], bool)]
    return UNAVAILABLE if not values else str(round(statistics.median(values), 1))


def _total(rows: list[dict[str, Any]], key: str) -> str:
    from decimal import Decimal

    # Unavailable when any row's value is: a total over runs some of which were not priced is no total
    # (M53 round 1 NB-1, as an arm's children).
    total = Decimal(0)
    for row in rows:
        try:
            total += Decimal(row.get(key))
        except Exception:  # noqa: BLE001 - unavailable, missing, or not money
            return UNAVAILABLE
    return str(total) if rows else UNAVAILABLE


def _cell(text: Any) -> str:
    """A table cell: one line, with the separator escaped."""
    return " ".join(str(text).split()).replace("|", "\\|")
