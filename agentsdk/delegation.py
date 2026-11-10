"""FR-185 (M52, P5A-D15 to P5A-D19): ad hoc delegation, the `delegate_task` tool.

A run whose spec names `delegate_task` and carries a `Delegation` can hand a piece of its task to a
child. Each call:

1. reads the files it names from the parent's workspace, through the provider's checked tar
   (FR-143) -- regular files only -- and builds the child's spec with them as its workspace's
   starting `files`, which checks FR-175's bounds against the child's `max_workspace_bytes`;
2. reserves `delegation_fraction` of the parent reservation's remainder for the child (FR-184);
3. runs the child through the parent's `SubagentPool` at depth 1, in a fresh workspace, with the
   parent's tenant, project and principal;
4. releases the child's lease when the child ends.

The parent receives the child's answer, run id and status, labelled with the child's provenance
(ADR-26). Every refusal is a tool error; a call refused at step 1 or 2 spawns nothing and takes no
task count. A child cannot delegate: its spec may not name the tool (P5A-D17).
"""

from __future__ import annotations

import json
import posixpath
import uuid
from dataclasses import dataclass, replace
from typing import Any

from .scope import ToolScope
from .tools import ApprovalPolicy, RiskClass, Tool, ToolOutput, ToolSpec

__all__ = ["DELEGATE_TOOL", "DELEGATED_ROLE", "MAX_DELEGATED_FILES", "Delegation", "delegate_tool"]

DELEGATE_TOOL = "delegate_task"
DELEGATED_ROLE = "delegate"
MAX_DELEGATED_FILES = 64
# A child's reservation is "delegated:" and a fresh id, named in the child's own manifest.
_RESERVATION_PREFIX = "delegated:"
_DEFAULT_TURNS = 20  # SUBAGENT_TURNS, the general subagent's documented turns (FR-177)


@dataclass(frozen=True)
class Delegation:
    """FR-185: what a delegating spec's children run as, their turns, and whether a spawn waits for
    approval."""

    agent: Any
    max_turns: int = _DEFAULT_TURNS
    approval: ApprovalPolicy = ApprovalPolicy.AUTO

    def __post_init__(self) -> None:
        from .api import AgentSpec

        if not isinstance(self.agent, AgentSpec):
            raise ValueError(f"Delegation.agent must be an AgentSpec, got {type(self.agent).__name__}")
        if self.agent.workspace is None:
            raise ValueError("Delegation.agent needs a workspace: a child works in its own")
        # NFR-54 (pre-review F2): a child starts with only the files its call names, so its workspace is
        # its own run's, never a plan's (which it would share with its parent) or a snapshot's copy.
        if self.agent.workspace.scope != "run" or self.agent.workspace.from_snapshot is not None:
            raise ValueError("Delegation.agent's workspace must be scope 'run' and start from no snapshot: "
                             "a child starts with only the files its call names")
        if DELEGATE_TOOL in self.agent.tool_profile or self.agent.delegate is not None:
            raise ValueError(f"Delegation.agent cannot delegate: a child has no {DELEGATE_TOOL} (P5A-D17)")
        if type(self.max_turns) is not int or self.max_turns < 1:
            raise ValueError(f"Delegation.max_turns must be an int of 1 or more, got {self.max_turns!r}")
        if not isinstance(self.approval, ApprovalPolicy):
            raise ValueError(f"Delegation.approval must be an ApprovalPolicy, got {self.approval!r}")


def _spec(approval: ApprovalPolicy) -> ToolSpec:
    from .workspace.tools import PATH

    return ToolSpec(
        name=DELEGATE_TOOL,
        description=(
            "Hand one self-contained piece of your task to a helper agent, which works in a workspace of its "
            "own and answers you with text. objective says exactly what to do and what to answer; files are "
            "paths in your workspace to copy into the helper's. Returns the helper's run id, status and "
            "answer. Calls in one turn run at the same time."),
        input_schema={"type": "object", "additionalProperties": False, "required": ["objective"],
                      "properties": {"objective": {"type": "string", "minLength": 1, "maxLength": 65536},
                                     "files": {"type": "array", "items": PATH, "maxItems": MAX_DELEGATED_FILES}}},
        risk_class=RiskClass.WRITE, read_only=False, idempotent=False, approval_policy=approval,
        # The child is bounded by its turns and its reservation, not by a clock.
        timeout_seconds=None, concurrency_safe=True)


async def named_files(binding: Any, paths: list[str]) -> dict[str, bytes]:
    """Step 1: each named path's regular file from the parent's workspace; a folder, a link or a
    missing path raises."""
    from .workspace.tools import one_file

    files: dict[str, bytes] = {}
    for path in paths:
        name = posixpath.normpath(path)
        if name not in files:
            files[name] = one_file(await binding.export(name), name)
    return files


def child_spec(agent: Any, files: dict[str, bytes]) -> Any:
    """The child's spec for one call: its own workspace, started with the named files (FR-175)."""
    return replace(agent, workspace=replace(agent.workspace, files=files))


def _briefing_text(objective: str, files: dict[str, bytes]) -> str:
    if not files:
        return objective
    return objective + "\n\nFiles copied into your workspace for this task: " + ", ".join(sorted(files)) + "."


def delegate_tool(delegation: Delegation, binding: Any, pool: Any, principal_context: Any) -> Tool:
    """The tool for one run: `binding` is its workspace, `pool` the SubagentPool its children run in."""
    from .subagents import Briefing

    async def delegate_task(objective: str, scope: ToolScope, files: list[str] | None = None) -> ToolOutput:
        named = await named_files(binding, list(files or ()))
        spec = child_spec(delegation.agent, named)
        lease = scope.budget_lease
        if lease is None:  # refused before the run starts; kept so a tool never runs unbudgeted
            raise ValueError(f"{DELEGATE_TOOL} needs a budget")
        # Pre-review F1: a fresh id per call, never the model's tool-call id, which a provider may repeat.
        child = lease.governor.reserve_child(lease.node_id, _RESERVATION_PREFIX + uuid.uuid4().hex)
        try:
            # The pool's task limit raises MaxDepthExceeded, which the executor reports as the call's error.
            result = await pool.spawn(
                parent=ToolScope(run_id=scope.run_id, tenant_id=scope.tenant_id, project_id=scope.project_id,
                                 ledger_run_id=scope.ledger_run_id),
                briefing=Briefing(objective=_briefing_text(objective, named), assigned_role=DELEGATED_ROLE,
                                  max_turns=delegation.max_turns),
                agent=spec, lease=child, depth=1, principal_context=principal_context)
        finally:
            child.release()
        report = {"run_id": result.run_id, "status": result.status.value, "answer": result.output,
                  "error": result.error}
        return ToolOutput(json.dumps(report), taken_in=(result.provenance,))

    return Tool(spec=_spec(delegation.approval), fn=delegate_task)
