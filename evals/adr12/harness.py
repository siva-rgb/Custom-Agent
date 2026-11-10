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
import contextlib
import hashlib
import pathlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from agentsdk.model import Usage
from agentsdk.presets import GENERAL_VERSION
from agentsdk.registry import call_cost
from agentsdk.workspace.docker import UID as WORKSPACE_UID

from . import report as report_module
from .sandbox import BUDGET_MULTIPLE, DELEGATION, M15_REPORT, M51_REPORT, SANDBOX, Workspace, ceilings
from .sandbox_arms import CLAUDE_UID
from .tasks import FIXTURE_ROOT

UNAVAILABLE = "unavailable"
M15_SETUP = {
    "agentsdk": "read, glob and grep over a host copy of the fixture, as in M15",
    "claude-agent-sdk": "Read, Glob and Grep over a host copy of the fixture, as in M15",
}
# The users differ as well as the tools (FR-179 asks the report to state each asymmetry; M50 round 1,
# note 2): read from both images' /etc/passwd on 2026-11-08, the uids from the constants that set them.
SANDBOX_SETUP = {
    "agentsdk": "the general preset in a workspace holding the task's files, egress by the task's allow list, "
                f"every export refused; it runs as uid {WORKSPACE_UID} (nobody, home /) in the workspace image, "
                "which has /bin/sh but no bash",
    "claude-agent-sdk": "Read, Glob, Grep, Bash, Write and Edit in a disposable container holding the task's files; "
                        f"it runs as uid {CLAUDE_UID} (agent, home /home/agent), and its image adds nodejs, npm "
                        "and bash (for the Bash tool) to the workspace image's base",
}
BUDGET_RULE = (f"this SDK's arm only: a USD and a token ceiling of {BUDGET_MULTIPLE} times the kind's median on "
               f"this SDK's arm in M15 ({M15_REPORT}); the sandbox kind {BUDGET_MULTIPLE} times the multi-step "
               f"median; the delegation kind {BUDGET_MULTIPLE} times the sandbox kind's median on this SDK's arm in "
               f"M51 ({M51_REPORT}), a child's share reserved from its parent's. The Claude arm has no budget")
# FR-188: the delegation kind's setup on each arm, and how its rows' children are counted.
DELEGATION_SETUP = {
    "agentsdk": SANDBOX_SETUP["agentsdk"] + "; the delegating preset (delegate=True), each child a general subagent "
                                            "in a workspace of its own with the files its call named",
    "claude-agent-sdk": SANDBOX_SETUP["claude-agent-sdk"] + "; and its CLI's own subagent tool, Agent",
}
DELEGATION_MEASURE = ("each row reports whether its run delegated and how many children it ran, never a condition "
                      "of passing: on this SDK's arm the runs whose parent_run_id is the run's, and its usage the "
                      "whole run's, children included; on the Claude arm its Agent calls whose result was not an "
                      "error, and its usage as the CLI's result reports it, which leaves out its subagents': so a "
                      "Claude run that called Agent at all reports its tokens unavailable and its cost as the CLI's "
                      "total_cost_usd, which counts them, or unavailable when the CLI gave none")
NETWORKING = ("this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm "
              "runs with ordinary networking, to reach the gateway and the fetch tasks' host")
# DECISION-7830ca10 (M50 round 1, note 3): a page reproduced from memory is no fetch.
FETCH_PROOF = ("a fetch task passes only with evidence the run reached its host, kept in each row's fetch_evidence: "
               "on this SDK's arm an allowed connection its workspace's proxy recorded, by id; on the Claude arm, "
               "whose network is not recorded, a call whose result was not an error, read as the shell would run "
               "it: WebFetch of the host's URL, or a Bash command whose program, after any wrapper, is curl or "
               "wget with the host among its arguments, or python or node running code that uses an HTTP client "
               "and names the host's URL (inline, a heredoc, or a script the run wrote). The Claude arm's evidence "
               "is the command the run gave, not the network: a URL computed at run time or a script edited after "
               "it was written is a fetch it does not count (pre-review F1, round-1 F1)")


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
    sandbox_arms: list[Any] = (),
    sandbox_tasks: Any = (),
    image_digests: dict[str, str] | None = None,
    verifier: Any = None,
    witness: tuple[bytes, str] | None = None,
    sweep: Any = None,
) -> Comparison:
    """Run every task on every arm, write the report, and return the rows.

    FR-179: the M15 tasks run on `arms` as M15 ran them; the sandbox tasks run on `sandbox_arms`,
    each checked by the workspace it left (DECISION-d0269633). A code task is checked by
    `verifier`'s run of its cases, a fetch task against `witness`, the page body the harness
    fetched itself and when (DECISION-b944d3ab). `sweep`, when given, removes whatever containers
    the comparison's labels still name once it ends."""
    reports_dir, workdir = pathlib.Path(reports_dir), pathlib.Path(workdir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    tasks, sandbox_tasks = list(tasks), list(sandbox_tasks)
    if sandbox_tasks and not sandbox_arms:
        raise ValueError("sandbox tasks need the sandbox arms")
    if any(task.verification for task in sandbox_tasks) and verifier is None:
        raise ValueError("code tasks need the verifier")
    if any(task.group == "fetch" for task in sandbox_tasks) and witness is None:
        raise ValueError("fetch tasks need the harness's witness")
    rows: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {"date": (stamp or datetime.now(timezone.utc).isoformat(timespec="seconds"))}
    try:
        for repetition in range(1, repetitions + 1):
            for task in tasks:
                for arm in arms:
                    rows.append(_one_run(arm, task, repetition, pricing, workdir))
            for task in sandbox_tasks:
                for arm in sandbox_arms:
                    rows.append(_one_sandbox_run(arm, task, repetition, pricing, verifier, witness))
    finally:
        # Pre-review F6: what was measured is written even when a run raised; F4: nothing is left.
        if sweep is not None:
            with contextlib.suppress(Exception):
                metadata["swept"] = asyncio.run(sweep())
        if len(rows) < repetitions * (len(tasks) * len(arms) + len(sandbox_tasks) * len(sandbox_arms)):
            metadata["incomplete"] = f"{len(rows)} runs measured before the comparison stopped"
            report_module.write_report({**metadata, "arms": [a.name for a in [*arms, *sandbox_arms]]}, rows,
                                       reports_dir)
    every = [*tasks, *sandbox_tasks]
    kinds = sorted({task.kind for task in every})
    names = list(dict.fromkeys([arm.name for arm in arms] + [arm.name for arm in sandbox_arms]))
    metadata = {
        **metadata,
        "gateway_model": gateway_model,
        "witness": ({"url": "https://example.com/", "fetched_at": witness[1], "bytes": len(witness[0]),
                     "sha256": hashlib.sha256(witness[0]).hexdigest()} if witness else None),
        "fetch_proof": FETCH_PROOF if any(task.group == "fetch" for task in sandbox_tasks) else None,
        "verification": ("code tasks verified by the harness in a fresh container of the workspace image with no "
                         "network, on visible and hidden cases" if verifier is not None else None),
        "arms": names,
        "versions": {arm.name: arm.version for arm in [*arms, *sandbox_arms]},
        "wire_formats": {arm.name: arm.wire_format for arm in [*arms, *sandbox_arms]},
        "repetitions": repetitions,
        "tasks": [task.id for task in every],
        # FR-179: the report states the task count, and every kind's setup and budget.
        "task_count": len(every),
        "kinds": {kind: sum(1 for task in every if task.kind == kind) for kind in kinds},
        "setups": {kind: {SANDBOX: SANDBOX_SETUP, DELEGATION: DELEGATION_SETUP}.get(kind, M15_SETUP) for kind in kinds},
        "delegation": DELEGATION_MEASURE if DELEGATION in kinds else None,
        "budget_rule": BUDGET_RULE,
        "ceilings": {kind: dict(zip(("usd", "tokens"), ceilings(kind))) for kind in kinds},
        "networking": NETWORKING if sandbox_tasks else None,
        "preset_version": GENERAL_VERSION if sandbox_tasks else None,
        "images": dict(image_digests or {}),
        "pricing": pricing.to_json() if hasattr(pricing, "to_json") else str(pricing),
    }
    json_path, markdown_path = report_module.write_report(metadata, rows, reports_dir)
    return Comparison(rows=rows, metadata=metadata, json_path=json_path, markdown_path=markdown_path)


def _one_run(arm: Any, task: Any, repetition: int, pricing: Any, workdir: pathlib.Path) -> dict[str, Any]:
    root = workdir / f"{task.id}-{arm.name}-{repetition}"
    if root.exists():  # pragma: no cover - a repeated stamp would hide a run
        shutil.rmtree(root)
    shutil.copytree(FIXTURE_ROOT, root)
    try:
        outcome = asyncio.run(arm.run(task, root))
    except Exception as exc:  # noqa: BLE001 - pre-review F6: a run that raised is a failed row
        from .arms import RunOutcome

        outcome = RunOutcome(answer=None, status="failed", turns=0, usage=Usage(), wall_ms=0.0,
                             error=f"the arm raised {type(exc).__name__}")
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
        "budget_usd": outcome.budget_usd or UNAVAILABLE,
        "budget_tokens": outcome.budget_tokens if outcome.budget_tokens is not None else UNAVAILABLE,
    }


def _one_sandbox_run(arm: Any, task: Any, repetition: int, pricing: Any, verifier: Any = None,
                     witness: tuple[bytes, str] | None = None) -> dict[str, Any]:
    """FR-179: one sandbox run, checked by the workspace it left, read back by the arm. A code task
    is verified by the harness, a fetch task judged against its witness (DECISION-b944d3ab)."""
    try:
        outcome = asyncio.run(arm.run_sandbox(task))
    except Exception as exc:  # noqa: BLE001 - pre-review F6: a run that raised is a failed row
        from .sandbox_arms import SandboxOutcome

        outcome = SandboxOutcome(answer=None, status="failed", turns=0, usage=Usage(), wall_ms=0.0,
                                 error=f"the arm raised {type(exc).__name__}")
    verified = None
    if task.verification is not None and outcome.files:
        verified = asyncio.run(verifier.verify(task, dict(outcome.files)))
    usage = outcome.usage
    # M53 round 1 NB-1: a usage that leaves out what the arm could not see is no count and no price;
    # the run's cost is then the arm's own total, and unavailable when it gave none (NFR-11).
    partial = getattr(outcome, "usage_partial", False)
    tokens = {key: UNAVAILABLE if partial else getattr(usage, key)
              for key in ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")}
    cost = outcome.sdk_cost_estimate if partial else call_cost(usage, pricing)
    space = Workspace(files=dict(outcome.files), answer=outcome.answer, verified=verified,
                      witness=witness[0] if witness and task.group == "fetch" else None,
                      fetched=frozenset(getattr(outcome, "fetched", frozenset())))
    return {
        "verified": verified if verified is not None else UNAVAILABLE,
        "task": task.id,
        "kind": task.kind,
        "group": task.group,
        "arm": arm.name,
        "repetition": repetition,
        "passed": bool(task.checker(space)),
        "status": outcome.status,
        "turns": outcome.turns,
        **tokens,
        "wall_ms": round(outcome.wall_ms, 1),
        "cost_usd": _money(cost),
        "sdk_cost_estimate_usd": _money(outcome.sdk_cost_estimate),
        "files_read_back": len(outcome.files),
        "fetched": sorted(getattr(outcome, "fetched", frozenset())),
        "fetch_evidence": list(getattr(outcome, "fetch_evidence", ())),
        # FR-188: whether the run delegated and how many children it ran -- measured, never a pass
        # condition, and only on a delegation task: no other run is given a tool to delegate with
        # (M53 pre-review F3, NFR-53).
        **({"children": UNAVAILABLE if getattr(outcome, "children", None) is None else outcome.children,
            "delegated": UNAVAILABLE if getattr(outcome, "children", None) is None else outcome.children > 0}
           if task.kind == DELEGATION else {}),
        "exports_refused": outcome.exports_refused if outcome.exports_refused is not None else UNAVAILABLE,
        "run_id": outcome.run_id or UNAVAILABLE,
        "workspace_id": outcome.workspace_id or UNAVAILABLE,
        "budget_usd": outcome.budget_usd or UNAVAILABLE,
        "budget_tokens": outcome.budget_tokens if outcome.budget_tokens is not None else UNAVAILABLE,
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
