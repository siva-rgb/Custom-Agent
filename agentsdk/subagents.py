"""FR-70, FR-71, FR-72 (M18): subagents, and the pool that runs them.

A subagent is a run of its own, linked to its parent by `runs.parent_run_id` and briefed
by that parent alone. Topology is hub and spoke: a child hears from its parent and
answers to its parent, and children never address one another.

The briefing is curated rather than inherited. A child receives its node's objective and
the contents of its `input_refs`, resolved through the run's artifact store, and nothing
else of the parent's history. What it answers carries the provenance of those inputs at
their maximum taint (ADR-26, `ContentProvenance.from_model`), so a tainted input cannot
launder itself by crossing a run boundary.

The pool owns the limits FR-72 names: how deep a chain of children may go, how many
tasks one run may spawn, and how many may run at once. The orchestrator M19 brings is
its only intended caller; M17's clarification applies here too, that a component arrives
before the thing that will drive it (DECISION-35f4c3f4).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from agentsdk.api import AgentSpec, RunConfig, Runner, RunStatus
from agentsdk.errors import MaxDepthExceeded, describe_exception
from agentsdk.identity import PrincipalContext
from agentsdk.postgres import RunScope
from agentsdk.primitives import ContentProvenance, Role
from agentsdk.context_policy import ContextPolicy
from agentsdk.scheduler import SchedulerLimits

__all__ = [
    "MAX_SUBAGENT_DEPTH",
    "Briefing",
    "SubagentPool",
    "SubagentResult",
]

# P2-D21: the orchestrator is depth 0, so a spawn at depth 3 is refused. Hub and spoke
# with one nesting, which also bounds ADR-06's overshoot.
MAX_SUBAGENT_DEPTH = 3
_DEFAULT_MAX_TURNS = 4


@dataclass(frozen=True, kw_only=True)
class Briefing:
    """What a parent tells a child, and nothing more (FR-70)."""

    objective: str
    assigned_role: str
    input_refs: tuple[str, ...] = ()
    expected_output_schema: Mapping[str, Any] | None = None
    max_turns: int = _DEFAULT_MAX_TURNS

    def __post_init__(self) -> None:
        for name in ("objective", "assigned_role"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty str, got {value!r}")
        if not isinstance(self.input_refs, (list, tuple)) or not all(
            type(ref) is str and ref for ref in self.input_refs
        ):
            raise ValueError("input_refs must be a list or tuple of non-empty str")
        object.__setattr__(self, "input_refs", tuple(self.input_refs))
        # FR-80 (M18a): refused here, so a bad schema never reaches spawn, takes no
        # task count in _admit and writes no row (round 4, C2).
        schema = self.expected_output_schema
        if schema is not None and not isinstance(schema, Mapping):
            raise ValueError(
                f"expected_output_schema must be a mapping or None, got {type(schema).__name__}"
            )
        if type(self.max_turns) is not int or self.max_turns < 1:
            raise ValueError(f"max_turns must be an int of 1 or more, got {self.max_turns!r}")

    @classmethod
    def from_node(cls, node: Any, *, max_turns: int = _DEFAULT_MAX_TURNS) -> Briefing:
        """The briefing a `PlanNode` describes (FR-64)."""
        schema = node.expected_output_schema
        return cls(
            objective=node.objective,
            assigned_role=node.assigned_role,
            input_refs=tuple(node.input_refs),
            expected_output_schema=None if schema is None else dict(schema),
            max_turns=max_turns,
        )


@dataclass(frozen=True, kw_only=True)
class SubagentResult:
    """What a child returns to its parent (FR-70).

    `RunResult` is what one run returns and carries no provenance; a node needs the
    child's run id, its answer, and the provenance that answer inherits from its inputs
    (DECISION-35f4c3f4).
    """

    run_id: str
    status: RunStatus
    output: str | None
    provenance: ContentProvenance
    artifacts: tuple[Any, ...] = ()
    error: str | None = None
    node_id: str | None = None


class SubagentPool:
    """Runs children for one parent, under FR-72's limits.

    The limits are per parent run: `max_tasks_per_run` counts the children one run has
    spawned, and `max_concurrent_subagents` bounds how many of them run at once. Depth
    is carried on the child's `RunConfig` and checked here.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        limits: SchedulerLimits | None = None,
        artifact_store: Any | None = None,
        context_policy: ContextPolicy | None = None,
    ) -> None:
        if context_policy is not None and not isinstance(context_policy, ContextPolicy):
            raise ValueError(f"context_policy must be a ContextPolicy or None, got {type(context_policy).__name__}")
        self._runner = runner
        # FR-77 (M20): what every child sees. It briefs them -- the briefing rules live
        # in the policy -- and rides on each child's RunConfig. None briefs the same way
        # and leaves a child seeing what it saw before M20 (DECISION-468e2bfa).
        self._policy = context_policy
        self._limits = limits if limits is not None else SchedulerLimits()
        self._artifacts = artifact_store
        self._lock = asyncio.Lock()
        self._spawned: dict[str, int] = {}
        self._slots: dict[str, asyncio.Semaphore] = {}

    def spawned(self, parent_run_id: str) -> int:
        """How many children this pool has spawned for that run."""
        return self._spawned.get(parent_run_id, 0)

    async def spawn(
        self,
        *,
        parent: RunScope,
        briefing: Briefing,
        agent: AgentSpec,
        lease: Any | None = None,
        depth: int = 1,
        principal_context: PrincipalContext | None = None,
        node_id: str | None = None,
    ) -> SubagentResult:
        """Run one child for `parent` and return what it answered.

        Refused before anything starts, with `MaxDepthExceeded` naming the limit, when
        the chain would be too deep or the run has already spawned its allowance: FR-72
        says the task limit refuses "the same way" as the depth limit, and that is read
        as the same error. A refusal fails its own node and leaves its siblings alone,
        so it raises to the caller rather than ending the parent run.
        """
        if type(depth) is not int or depth < 1:
            raise ValueError(f"depth must be an int of 1 or more, got {depth!r}")
        if depth >= MAX_SUBAGENT_DEPTH:
            raise MaxDepthExceeded(
                f"a subagent at depth {depth} would pass the limit of {MAX_SUBAGENT_DEPTH}"
            )
        slot = await self._admit(parent.run_id)
        async with slot:
            return await self._run_child(parent, briefing, agent, lease, depth, principal_context, node_id)

    async def _admit(self, parent_run_id: str) -> asyncio.Semaphore:
        async with self._lock:
            spawned = self._spawned.get(parent_run_id, 0)
            if spawned >= self._limits.max_tasks_per_run:
                raise MaxDepthExceeded(
                    f"run {parent_run_id} has spawned {spawned} tasks, its max_tasks_per_run"
                )
            self._spawned[parent_run_id] = spawned + 1
            if parent_run_id not in self._slots:
                self._slots[parent_run_id] = asyncio.Semaphore(self._limits.max_concurrent_subagents)
            return self._slots[parent_run_id]

    async def _run_child(
        self,
        parent: RunScope,
        briefing: Briefing,
        agent: AgentSpec,
        lease: Any | None,
        depth: int,
        principal_context: PrincipalContext | None,
        node_id: str | None,
    ) -> SubagentResult:
        # The list is built here rather than returned, so an input that fails after
        # earlier ones resolved still reports their taint: a result that says
        # TRUSTED_SOURCE about tainted inputs is wrong even when it carries no text
        # (FR-70, round 1, L1).
        briefed: list[tuple[str, ContentProvenance]] = []
        try:
            task = await (self._policy or ContextPolicy()).brief(briefing, self._artifacts, briefed)
        except Exception as exc:  # noqa: BLE001 - a briefing that cannot be built fails its node
            return SubagentResult(
                run_id="", status=RunStatus.FAILED, output=None,
                provenance=ContentProvenance.from_model(*(p for _, p in briefed)),
                error=describe_exception(exc), node_id=node_id,
            )
        config = RunConfig(
            # FR-43, FR-72: the manifest is the record of how far a run could fan out,
            # so a child records the pool's limits rather than the Runner's defaults
            # (round 1, L2). Provider limits are shared by every run of a Runner and a
            # RunConfig may not carry them, so they stay where they were set.
            scheduler_limits=replace(self._limits, provider_concurrency_limits={}),
            tenant_id=parent.tenant_id,
            project_id=parent.project_id,
            max_turns=briefing.max_turns,
            parent_run_id=parent.run_id,
            # FR-90, FR-92 (M22): every child shares its top-level run's ledger.
            ledger_run_id=getattr(parent, "ledger_run_id", None) or parent.run_id,
            principal_context=principal_context,
            budget_lease=lease,
            depth=depth,
            output_schema=None if briefing.expected_output_schema is None else dict(briefing.expected_output_schema),
            briefed_inputs=tuple(briefed),
            node_id=node_id,
            context_policy=self._policy,
        )
        provenances = [p for _, p in briefed]
        result = await self._runner.run(agent, task, config)
        # FR-70: a child's inputs are its briefing AND whatever it read while running.
        # Round 2 found a child fetching a page through a tool and handing the text to
        # its parent labelled clean, which is the laundering FR-70 forbids (M1).
        child = RunScope(run_id=result.run_id, tenant_id=parent.tenant_id, project_id=parent.project_id)
        try:
            taken_in = await self._what_it_read(child)
        except Exception as exc:  # noqa: BLE001
            # The answer cannot be labelled, and a label this pool cannot justify is
            # worse than no answer: the node fails rather than passing text off as
            # clean. Declared in the M18 round 3 brief.
            return SubagentResult(
                run_id=result.run_id, status=RunStatus.FAILED, output=None,
                provenance=ContentProvenance.from_model(*provenances),
                error=f"the child's history could not be read, so its answer cannot be labelled: "
                      f"{describe_exception(exc)}",
                node_id=node_id,
            )
        return SubagentResult(
            run_id=result.run_id,
            status=result.status,
            output=result.output,
            # ADR-26: what a model made of these inputs carries their taint, at the
            # taint of the most tainted and the trust of the least trusted.
            provenance=ContentProvenance.from_model(*provenances, *taken_in),
            error=result.error,
            node_id=node_id,
        )

    async def _what_it_read(self, child: RunScope) -> tuple[ContentProvenance, ...]:
        """The provenance of every tool result in the child's own history (FR-40).

        Offloaded, because it is a store call: on Postgres it checks out a pooled
        connection and runs a SELECT, and FR-20 and DECISION-6b62d1f5 say every store
        call made during a run goes to a worker thread. Round 3 found this one reading
        on the event loop after every completed child, while the pool held a
        concurrency slot across it -- the third recurrence of KNOWLEDGE-c0f23fea, where
        the offload was applied to the call sites someone looked at rather than
        asserted of every call.
        """
        history = await asyncio.to_thread(self._runner._history_for, child)
        return tuple(
            result.provenance
            for message in history
            if message.role is Role.TOOL
            for result in message.tool_results
        )
