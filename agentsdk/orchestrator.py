"""FR-73 to FR-75 (M19): the orchestrator.

The orchestrator is one agent run at depth 0 whose agent has one tool, `run_plan`
(DECISION-c8d0932a). The model submits a plan as the tool's arguments; the tool stores it
as a `PlanVersion`, runs its DAG through a `SubagentPool` with every child linked to this
run, checks each node's acceptance criteria, and answers with each node's outcome. A plan
that fails comes back to the model, whose next call of the tool is the replan -- inside
the same run, counted against the orchestrator's reserve, bounded by `max_replans`.

Application code still calls `Runner.run()` and nothing else: an `Orchestrator` supplies
the tool to register, the agent to run and a `RunConfig` holding the run's budget.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .api import AgentSpec, RunConfig, RunStatus
from .budget import BudgetGovernor, BudgetPolicy
from .errors import MaxDepthExceeded, describe_exception
from .events import EventType
from .executor import ToolExecutor
from .loop import schema_problem
from .outcomes import Completed
from .plan import PLAN_SCHEMA, PlanVersion, _node_document, _thawed, plan_from_document
from .postgres import RunScope
from .primitives import ContentProvenance, ToolCall
from .scheduler import SchedulerLimits
from .context_policy import ContextPolicy
from .scope import ToolScope
from .subagents import Briefing, SubagentPool
from .tools import Tool, ToolOutput, ToolSpec

__all__ = ["PLAN_TOOL", "Orchestrator"]

PLAN_TOOL = "run_plan"
_ARTIFACT_URN = "urn:agentsdk:artifact:"
# FR-75 and DECISION-c8d0932a: every failure replans except these.
_NOT_REPLANNABLE = ("budget_exceeded", "cancelled")

_INSTRUCTIONS = (
    "You are an orchestrator. Break the task into a plan of nodes and submit it by calling "
    f"{PLAN_TOOL} once. Each node names an assigned_role, an objective, and the node_ids it "
    "depends on; a node receives its dependencies' outputs as inputs. The tool runs the plan "
    "and reports each node's outcome. If it reports that the plan failed and a replan is "
    f"allowed, call {PLAN_TOOL} again with a corrected plan; reuse a node_id to keep a node "
    "that is already done. When the plan is done, answer with the result."
)


@dataclass
class _Run:
    """What one orchestrator run has done so far."""

    plan_id: str
    pool: SubagentPool
    versions: list[PlanVersion] = field(default_factory=list)
    # node_id -> (the version it was done in, its definition then, whether it had side
    # effects), kept for the run's whole life. Round 2, D5: a record one version deep
    # forgot a done side-effecting node as soon as one version omitted it, and the next
    # version to name it ran it again.
    done: dict[str, tuple[int, Any, bool]] = field(default_factory=dict)
    children: set[str] = field(default_factory=set)  # every child run this run spawned
    outputs: dict[str, str] = field(default_factory=dict)  # node_id -> output artifact id
    # node_id -> what its child answered when it was last done, and that answer's label.
    # One record, so text and label cannot drift apart across versions (round 1, D3).
    answers: dict[str, tuple[str, ContentProvenance]] = field(default_factory=dict)
    replans: int = 0
    ended: str | None = None  # why no further version may run


class Orchestrator:
    """FR-73: runs plans for orchestrator runs, through the `run_plan` tool.

    `roles` maps each node's `assigned_role` to the AgentSpec its child runs as; a node
    naming a role not in it fails. `policy` is each run's budget (FR-67), and `limits`
    bound the children (FR-72).
    """

    def __init__(
        self,
        *,
        roles: Mapping[str, AgentSpec],
        policy: BudgetPolicy,
        limits: SchedulerLimits | None = None,
        instructions: str = _INSTRUCTIONS,
        context_policy: ContextPolicy | None = None,
    ) -> None:
        if not isinstance(roles, Mapping) or not roles or not all(
            type(name) is str and name and isinstance(spec, AgentSpec) for name, spec in roles.items()
        ):
            raise ValueError("roles must be a non-empty mapping of role names to AgentSpec")
        if not isinstance(policy, BudgetPolicy):
            raise ValueError(f"policy must be a BudgetPolicy, got {type(policy).__name__}")
        if limits is not None and not isinstance(limits, SchedulerLimits):
            raise ValueError(f"limits must be a SchedulerLimits or None, got {type(limits).__name__}")
        if context_policy is not None and not isinstance(context_policy, ContextPolicy):
            raise ValueError(f"context_policy must be a ContextPolicy or None, got {type(context_policy).__name__}")
        self._roles = dict(roles)
        # FR-77, FR-78 (M20): what the orchestrator and every child see, and when their
        # histories compact. The default compacts at 0.75 of the model's window, so a
        # model the ModelRegistry does not know needs a policy naming its window
        # (DECISION-468e2bfa).
        self.context_policy = context_policy if context_policy is not None else ContextPolicy()
        self._policy = policy
        self._limits = limits if limits is not None else SchedulerLimits()
        self._runs: dict[str, _Run] = {}
        schema = {key: value for key, value in PLAN_SCHEMA.items() if key not in ("$schema", "$id")}
        self.tool = Tool(
            spec=ToolSpec(
                name=PLAN_TOOL,
                description="Run a plan of nodes, each executed by a subagent, and report each node's outcome.",
                input_schema=schema,
                # The whole DAG runs inside this one call; the run's budget and turn
                # limit bound it, not a per-call clock.
                timeout_seconds=None,
            ),
            fn=self._run_plan,
        )
        self.agent = AgentSpec(id="orchestrator", instructions=instructions, tool_profile=(PLAN_TOOL,))

    def config(self, *, tenant_id: str, project_id: str, **options: Any) -> RunConfig:
        """A RunConfig for one orchestrator run, holding a fresh budget for it.

        The orchestrator's own calls draw on its reserve from the first one, before any
        plan exists (FR-73): planning is counted, not free.
        """
        governor = BudgetGovernor.for_run(self._policy)
        return RunConfig(
            tenant_id=tenant_id, project_id=project_id, budget_lease=governor.orchestrator_lease(),
            # Unresolved from the start: only a plan version that completes clears it, so
            # a run that never gets one through ends failed (FR-75, round 1 D1).
            pending_failure="no plan version completed", context_policy=self.context_policy, **options,
        )

    def plans(self, run_id: str) -> tuple[PlanVersion, ...]:
        """Every plan version this orchestrator ran for `run_id`, oldest first."""
        found = self._runs.get(run_id)
        return () if found is None else tuple(found.versions)

    # --- the tool ------------------------------------------------------------------------------

    async def _run_plan(self, *, scope: ToolScope, **document: Any) -> ToolOutput:
        lease = scope._lease
        if lease is None or getattr(lease, "governor", None) is None or scope._runner is None:
            raise ValueError(f"{PLAN_TOOL} runs only in a run started with Orchestrator.config()")
        governor = lease.governor
        run = self._runs.get(scope.run_id)
        if run is None:
            run = _Run(
                plan_id=str(uuid.uuid4()),
                pool=SubagentPool(
                    scope._runner, limits=self._limits, artifact_store=scope.artifacts,
                    context_policy=self.context_policy,
                ),
            )
            self._runs[scope.run_id] = run
        if run.ended is not None:
            raise ValueError(f"this run's plan has ended and cannot run again: {run.ended}")
        # FR-75, round 1 D1: the run is unresolved from its first turn (pending_failure, set
        # by config()). Every way out of _submit either reaches _report, which clears that
        # for a done version and sets it for any other, or raises into the line below; a
        # cancellation ends the run cancelled. So nothing here can leave it completed.
        try:
            return await self._submit(run, scope, governor, document)
        except Exception as exc:
            self._unresolved(scope, f"plan {run.plan_id} failed: {describe_exception(exc)}")
            raise

    @staticmethod
    def _unresolved(scope: ToolScope, reason: str | None) -> None:
        if scope._control is not None:
            scope._control.failure = reason

    async def _submit(self, run: _Run, scope: ToolScope, governor: BudgetGovernor, document: Any) -> ToolOutput:
        previous = run.versions[-1] if run.versions else None
        if previous is not None:
            # FR-75: a replan only while the count is under its limit and budget remains.
            if run.replans >= self._policy.max_replans:
                raise ValueError(self._end(run, scope, f"max_replans {self._policy.max_replans} reached"))
            if not governor.may_start_child():
                raise ValueError(self._end(run, scope, "budget_exceeded"))
        plan = plan_from_document(
            document,
            plan_id=run.plan_id,
            version=1 if previous is None else previous.version + 1,
            run_id=scope.run_id,
            parent_plan=None if previous is None else (previous.plan_id, previous.version),
        )
        carried = self._carried(run, plan)
        governor.adopt(plan, carried=carried)
        states = scope._runner._run_states_for(scope.tenant_id, scope.project_id)
        await states.put_plan(plan)
        if previous is not None:
            run.replans += 1
        run.versions.append(plan)
        statuses = {node.node_id: "pending" for node in plan.nodes}
        reasons: dict[str, str] = {}
        for node_id in carried:
            await states.transition(
                plan.plan_id, plan.version, node_id, "done", sink=scope._events,
                reason=f"carried from version {run.done[node_id][0]}",
            )
            statuses[node_id] = "done"
        labels: dict[str, tuple[ContentProvenance, ...]] = {}
        try:
            await self._execute(run, plan, scope, governor, states, statuses, reasons, labels)
        except asyncio.CancelledError:
            run.ended = "cancelled"
            raise
        finally:
            # Recorded however this version ends: a node that finished before a raise or a
            # cancellation has had its side effects all the same.
            for node in plan.nodes:
                if statuses[node.node_id] == "done" and node.node_id not in carried:
                    run.done[node.node_id] = (plan.version, _node_document(node), node.side_effecting)
        return self._report(run, plan, scope, governor, statuses, reasons, labels)

    def _carried(self, run: _Run, plan: PlanVersion) -> tuple[str, ...]:
        """FR-75: the nodes of the new version that were done in any earlier version of
        this run, and so are not rerun.

        A node that had side effects when it was done is carried whatever the new version
        says of it, and however many versions ago it ran; a node without side effects is
        carried only when the new version describes it as it was when done, and otherwise
        runs again. Read from the run's whole record, not the version before (D5).
        """
        carried = []
        for node in plan.nodes:
            record = run.done.get(node.node_id)
            if record is None:
                continue
            _, document, side_effecting = record
            if side_effecting or document == _node_document(node):
                carried.append(node.node_id)
        return tuple(carried)

    def _end(self, run: _Run, scope: ToolScope, why: str) -> str:
        """No further version may run: the run will end failed with its last plan."""
        last = run.versions[-1]
        reason = f"plan {last.plan_id} version {last.version} failed and cannot be replanned: {why}"
        run.ended = reason
        if scope._control is not None:
            scope._control.failure = reason
        return reason

    # --- running a version ---------------------------------------------------------------------

    async def _execute(
        self, run: _Run, plan: PlanVersion, scope: ToolScope, governor: BudgetGovernor, states: Any,
        statuses: dict[str, str], reasons: dict[str, str], labels: dict[str, tuple[ContentProvenance, ...]],
    ) -> None:
        """FR-73: ready nodes run concurrently, a dependent only after every dependency is
        done, and a node whose dependency did not finish is skipped. Cancelling this call
        cancels every running child and awaits it."""
        running: dict[asyncio.Task[Any], str] = {}

        async def settle(node_id: str, status: str, reason: str | None) -> None:
            statuses[node_id] = status
            if reason is not None:
                reasons[node_id] = reason
            await states.transition(plan.plan_id, plan.version, node_id, status, sink=scope._events, reason=reason)

        try:
            while True:
                progressed = False
                for node in plan.nodes:
                    if statuses[node.node_id] != "pending":
                        continue
                    blocked = [d for d in node.dependencies if statuses[d] in ("failed", "skipped", "cancelled")]
                    if blocked:
                        progressed = True
                        await settle(node.node_id, "skipped", f"dependency {blocked[0]} did not finish")
                    elif all(statuses[d] == "done" for d in node.dependencies):
                        progressed = True
                        await settle(node.node_id, "ready", None)
                        await settle(node.node_id, "running", None)
                        running[asyncio.ensure_future(self._node(run, node, scope, governor))] = node.node_id
                if not running:
                    if all(status != "pending" for status in statuses.values()):
                        return
                    if not progressed:  # a DAG always has a next node; this guards the loop
                        raise RuntimeError(f"plan {plan.plan_id} version {plan.version} stopped making progress")
                    continue  # a skip above may have decided another node; look again
                finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    node_id = running.pop(task)
                    status, reason, labels[node_id] = task.result()
                    await settle(node_id, status, reason)
        except BaseException as exc:
            ending = exc
            # A fault in this version's own bookkeeping -- a transition the store refused --
            # is not a cancellation: the nodes already running are let finish, since one may
            # have run its child's tools already, and cancelling it would record it cancelled
            # and rerun it on the replan (rounds 3 and 4, D6). Only a cancellation cancels
            # them, and one arriving while they finish still does.
            if running and not isinstance(exc, asyncio.CancelledError):
                try:
                    await asyncio.wait(running)
                except asyncio.CancelledError as cancelled:
                    ending = cancelled
            # NFR-22: no child is left running once this call has returned or raised.
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            # A node whose task returned has had its side effects: it is recorded as it
            # ended, never as cancelled. settle() sets the status before it writes, so a
            # store that fails again still leaves the run's record true.
            for task, node_id in running.items():
                if task.cancelled() or task.exception() is not None:
                    continue
                status, reason, labels[node_id] = task.result()
                try:
                    await settle(node_id, status, reason)
                except Exception:  # noqa: BLE001 - the raise above decides how this ends
                    pass
            for node_id, status in statuses.items():
                if status not in ("done", "failed", "skipped", "cancelled"):
                    try:
                        await settle(node_id, "cancelled", None)
                    except Exception:  # noqa: BLE001 - the cancellation decides how this ends
                        pass
            if ending is exc:
                raise
            raise ending  # a cancellation that arrived while the nodes finished

    async def _node(
        self, run: _Run, node: Any, scope: ToolScope, governor: BudgetGovernor
    ) -> tuple[str, str | None, tuple[ContentProvenance, ...]]:
        """One node: its status, why, and the label of everything said about it.

        Total: a failure inside one node fails that node, with its reason, and leaves its
        siblings running (round 1, D1). Cancellation still passes through.
        """
        labels: list[ContentProvenance] = []
        try:
            status, reason = await self._attempt(run, node, scope, governor, labels)
        except Exception as exc:  # noqa: BLE001
            status, reason = "failed", f"node_error: {describe_exception(exc)}"
        return status, reason, tuple(labels)

    async def _attempt(
        self, run: _Run, node: Any, scope: ToolScope, governor: BudgetGovernor, labels: list[ContentProvenance]
    ) -> tuple[str, str | None]:
        role = self._roles.get(node.assigned_role)
        if role is None:
            return "failed", f"unknown_role: {node.assigned_role!r} is not one of this orchestrator's roles"
        if not governor.may_start_child():
            return "failed", "budget_exceeded"
        lease = governor.lease(node.node_id)
        try:
            result = await run.pool.spawn(
                parent=RunScope(run_id=scope.run_id, tenant_id=scope.tenant_id, project_id=scope.project_id),
                briefing=Briefing(
                    objective=node.objective,
                    assigned_role=node.assigned_role,
                    # DECISION-c8d0932a: a dependency's output is a briefed input, so
                    # its taint follows the edge (FR-70, FR-83).
                    input_refs=tuple(node.input_refs) + tuple(run.outputs[d] for d in node.dependencies),
                    expected_output_schema=_thawed(node.expected_output_schema),
                ),
                agent=role,
                lease=lease,
                depth=1,
                node_id=node.node_id,
            )
        except MaxDepthExceeded as exc:
            return "failed", f"spawn_refused: {describe_exception(exc)}"
        finally:
            lease.release()
        # Whatever this node's report says came from its child -- an answer, an error, a
        # reason -- carries the child's label (round 1, D2 and D3).
        labels.append(result.provenance)
        if result.run_id:
            run.children.add(result.run_id)
        if result.status is not RunStatus.COMPLETED:
            if result.error == "budget_exceeded":
                return "failed", "budget_exceeded"
            if result.error == "output_contract_violation" and any(
                c.kind == "output_schema" for c in node.acceptance_criteria
            ):
                # FR-74: the output_schema criterion is FR-71's check, so its failure is
                # the child's second invalid answer, reported as the criterion it is.
                return "failed", (
                    "acceptance_criterion_failed: output_schema: the answer did not validate "
                    "against expected_output_schema twice (output_contract_violation)"
                )
            return "failed", f"child_failed: {result.error or result.status.value}"
        problem = await self._criteria(run, node, result.output, scope, role, labels)
        if problem is not None:
            return "failed", problem
        store = scope._runner._artifacts_for(scope.tenant_id, scope.project_id)
        try:
            ref = await store.put(
                (result.output or "").encode("utf-8"),
                mime_type="text/plain",
                provenance=result.provenance,
                created_by_agent=role.id,
                source_run=result.run_id,
                source_task=node.node_id,
            )
        except Exception as exc:  # noqa: BLE001 - its dependents cannot be briefed without it
            return "failed", f"output_not_stored: {describe_exception(exc)}"
        run.outputs[node.node_id] = ref.artifact_id
        run.answers[node.node_id] = (result.output or "", result.provenance)
        return "done", None

    async def _criteria(
        self, run: _Run, node: Any, output: str | None, scope: ToolScope, role: AgentSpec,
        labels: list[ContentProvenance],
    ) -> str | None:
        """FR-74: the first criterion that is not satisfied, or None when every one is."""
        for criterion in node.acceptance_criteria:
            name = f"{criterion.kind} {criterion.target or criterion.description or ''}".strip()
            if criterion.kind == "critic":
                return f"criterion_not_available: {name}; a critic arrives with Phase 3"
            detail = await self._check(run, criterion, node, output, scope, role, labels)
            if detail is not None:
                return f"acceptance_criterion_failed: {name}: {detail}"
        return None

    async def _check(
        self, run: _Run, criterion: Any, node: Any, output: str | None, scope: ToolScope, role: AgentSpec,
        labels: list[ContentProvenance],
    ) -> str | None:
        if criterion.kind == "output_schema":
            if node.expected_output_schema is None:
                return "the node sets no expected_output_schema"
            return schema_problem(output, _thawed(node.expected_output_schema))
        if criterion.kind == "artifact_exists":
            target = criterion.target
            if not target.startswith(_ARTIFACT_URN):
                return f"{target!r} is not an artifact uri"
            artifact_id = target[len(_ARTIFACT_URN):]
            try:
                # The run's own: the store is bound to a tenant and project, not a run, so an
                # artifact any other run left there would otherwise satisfy this (round 2).
                ref = await scope.artifacts.metadata(artifact_id)
                if ref.source_run not in {scope.run_id, *run.children}:
                    return f"{target} is not an artifact of this run or its children"
                # get() verifies the content against its recorded content_hash (FR-55).
                await scope.artifacts.get(artifact_id)
            except Exception as exc:  # noqa: BLE001 - missing or tampered alike fail the criterion
                return describe_exception(exc)
            return None
        # tool_succeeds: through the normal executor, with the node's own permissions.
        runner = scope._runner

        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            await scope._control.store(scope._events.emit, EventType.TOOL_CALLED, payload)

        executor = ToolExecutor(
            registry=runner._registry,
            permission_checker=role.checker(),
            hook=runner._hook,
            emit=emit,
            scope=ToolScope(
                run_id=scope.run_id, tenant_id=scope.tenant_id, project_id=scope.project_id,
                node_id=node.node_id, artifacts=scope.artifacts,
            ),
        )
        outcome = await executor.execute(
            ToolCall(id=f"criterion-{uuid.uuid4()}", name=criterion.target, arguments=dict(criterion.arguments or {}))
        )
        if isinstance(outcome, Completed) and not outcome.result.is_error:
            return None
        result = getattr(outcome, "result", None)
        if result is None:
            return type(outcome).__name__
        # The tool's own text goes into the node's reason, so its label goes with it
        # (round 1, D2): a tool that read the web says so in what the orchestrator sees.
        labels.append(result.provenance)
        return result.content or type(outcome).__name__

    # --- the answer ----------------------------------------------------------------------------

    def _report(
        self, run: _Run, plan: PlanVersion, scope: ToolScope, governor: BudgetGovernor,
        statuses: dict[str, str], reasons: dict[str, str], labels: dict[str, tuple[ContentProvenance, ...]],
    ) -> ToolOutput:
        failed = {node_id: reason for node_id, reason in reasons.items() if statuses[node_id] == "failed"}
        # The report and its labels are built together, from the same records (round 1, D2
        # and D3): every label of what this version says about a node, and the answer of a
        # node that is done in this version -- run now or carried -- with the label stored
        # beside it. A node that is not done shows no answer, so no earlier version's text
        # can appear under a later version's label.
        taken_in: list[ContentProvenance] = [label for node_labels in labels.values() for label in node_labels]
        nodes = []
        for node in plan.nodes:
            item: dict[str, Any] = {"node_id": node.node_id, "status": statuses[node.node_id]}
            if node.node_id in reasons:
                item["reason"] = reasons[node.node_id]
            if statuses[node.node_id] == "done" and node.node_id in run.answers:
                item["output"], label = run.answers[node.node_id]
                taken_in.append(label)
                # Its output's uri, so a replan can name it in artifact_exists: a planner
                # cannot know an artifact's uri before it is written.
                item["artifact"] = _ARTIFACT_URN + run.outputs[node.node_id]
            nodes.append(item)
        report: dict[str, Any] = {"plan_id": plan.plan_id, "version": plan.version, "nodes": nodes}
        if all(status == "done" for status in statuses.values()):
            if scope._control is not None:
                scope._control.failure = None
            report["status"] = "done"
            report["next"] = "The plan is done. Answer with the result."
            return ToolOutput(json.dumps(report), taken_in=tuple(taken_in))
        report["status"] = "failed"
        replannable = not any(reason.startswith(_NOT_REPLANNABLE) for reason in failed.values())
        left = self._policy.max_replans - run.replans
        if replannable and left > 0 and governor.may_start_child():
            reason = f"plan {plan.plan_id} version {plan.version} failed and was not replanned"
            if scope._control is not None:
                # Until a later version succeeds, a final answer ends this run failed.
                scope._control.failure = reason
            report["next"] = f"Call {PLAN_TOOL} again with a corrected plan; {left} replan(s) remain."
        else:
            if not replannable:
                why = "a node ended budget_exceeded or cancelled, which does not replan"
            elif left <= 0:
                why = f"max_replans {self._policy.max_replans} reached"
            else:
                why = "budget_exceeded"
            report["next"] = f"The run has failed: {self._end(run, scope, why)}"
        return ToolOutput(json.dumps(report), taken_in=tuple(taken_in))
