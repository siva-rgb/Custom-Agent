"""The public API (FR-1, FR-48, NFR-5, LLD 3.3).

`Runner.run()` is the only method most application code calls. Everything else in
this package is an internal collaborator that Runner composes -- AgentLoop,
ToolExecutor, ContextAssembler, ModelClient. That visibility boundary is the
entire point of Runner: it is what lets Phase 2 replace the loop with an
orchestrator without any caller noticing.

Since M12, `Runner.start()` starts a run and returns a `RunHandle` for it at once,
through which a caller streams the run's events, reads its state, awaits its
result or cancels it. `Runner.run()` is a run started that way and awaited.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any, Mapping, Sequence

import uuid
from dataclasses import dataclass, field, replace
from enum import Enum

from .compaction import ContextCompactor
from .context import ContextAssembler
from .context_policy import ContextPolicy
from .errors import UnknownMcpServer, UnpricedModel, WorkspaceUnavailable, describe_exception
from .events import EventSink, EventType, InMemoryEventSink, RunEvent
from .executor import ToolExecutor
from .handle import PublishingSink, RunControl, RunHandle, RunState
from .hooks import RuntimeHook
from .identity import PrincipalContext
from .loop import AgentLoop, RunMeter, sent_model
from .manifest import build_manifest
from .model import ModelClient, ModelRequest, ReasoningEffort, Usage
from .permissions import AllowlistPermissionChecker, PermissionChecker
from .persistence import Persistence
from .postgres import RunScope, column_rejection_reason
from .artifacts import InMemoryArtifactStore
from .plan import InMemoryRunStateStore
from .scope import AttributedArtifacts, ToolScope
from .evidence import InMemoryEvidenceStore, ResourceCache
from .ledger import EvidenceLedger, InMemoryLedgerStore, RunLedger
from .gate import EvidenceVerification, GatePolicy, NodeSubject
from .interruptions import (
    ApprovalWaits,
    InMemoryInterruptionStore,
    InterruptionAlreadyDecided,
    InterruptionDecision,
    InterruptionExpired,
    InterruptionKindMismatch,
    InterruptionNotWaiting,
    InterruptionView,
    StandingDecision,
    UnknownInterruption,
)
from .calibration import CalibrationReport, _checked, calibrate
from .claimlock import ClaimLock
from .primitives import ContentProvenance, unstorable_reason
from .budget import BudgetLease, tokens_of
from .prices import PRICE_TABLE_DATE, shipped_pricing
from .registry import ModelRegistry, call_cost, default_registry
from .scheduler import ProviderSlots, RunSlots, SchedulerLimits
from .session import InMemorySessionStore, SessionStore
from .timings import elapsed_ms, now_ns, wall_clock
from .tools import Tool, ToolRegistry
from .version import __version__
from .workspace import WorkspaceProvider, WorkspaceSpec

__all__ = ["AgentSpec", "RunConfig", "RunHandle", "RunResult", "RunState", "RunStatus", "Runner"]


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"
    # FR-50, P2-D3 (DECISION-a44db7e4): a new terminal status rather than failed
    # with a reason, so a cancelled run can be told from a failure without
    # parsing its error.
    CANCELLED = "cancelled"


# The INTEGER column's ceiling, named once (see RunConfig.__post_init__).
_MAX_TURNS_CEILING = 2**31 - 1


def _refuse_bad_output_limit(value: object, owner: str) -> None:
    """max_output_tokens (FR-27), refused by name at construction like max_turns.

    Explicit about type rather than trusting a comparison: a bool passes every
    range check (True >= 1), and a string raises a TypeError that names nothing.
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{owner}.max_output_tokens must be an int or None, got {type(value).__name__}"
        )
    if not 1 <= value <= _MAX_TURNS_CEILING:
        raise ValueError(
            f"{owner}.max_output_tokens must be between 1 and {_MAX_TURNS_CEILING} "
            f"(execution_manifests.max_output_tokens is an INTEGER column), got {value}"
        )


def _reasoning_effort(value: object, owner: str) -> ReasoningEffort | None:
    """reasoning_effort (FR-28): a ReasoningEffort, its exact value, or None."""
    if value is None or isinstance(value, ReasoningEffort):
        return value
    if isinstance(value, str):
        try:
            return ReasoningEffort(value)
        except ValueError:
            pass
    raise ValueError(
        f"{owner}.reasoning_effort must be one of "
        f"{[effort.value for effort in ReasoningEffort]} or None, got {value!r}"
    )


def _refuse_bad_scheduler_limits(value: object, owner: str) -> None:
    """scheduler_limits (FR-43): a SchedulerLimits or None, refused by name."""
    if value is not None and not isinstance(value, SchedulerLimits):
        raise ValueError(
            f"{owner} scheduler_limits must be a SchedulerLimits or None, got {type(value).__name__}"
        )


def _cancellation_reason(value: object) -> str:
    """What a cancelled run records as its error (FR-50): the reason given, when it
    is text a store can hold, and `cancelled` otherwise."""
    if isinstance(value, str) and value:
        text = str.__getitem__(value, slice(None))
        if unstorable_reason(text) is None:
            return text
    return "cancelled"


@dataclass(frozen=True)
class AgentSpec:
    id: str
    instructions: str
    name: str = ""
    role: str = ""
    preferred_model: str | None = None
    # Names selected from the Runner's registry, assigned ad hoc per spawn
    # (ADR-14). An EMPTY profile permits nothing -- see checker(). It does not
    # mean "everything"; an empty allowlist that opened the gates would fail
    # silently open, which is the wrong direction for a permission default.
    #
    # The profile gates EXECUTION, and under a ContextPolicy (M20, FR-77) also
    # VISIBILITY: the agent is sent only its profile's tools. Without a policy the
    # model is still offered every registered tool's schema, as in Phase 0, which
    # keeps AC-2's permission-denial path reachable. Declared (M21, C5): a
    # permission_policy below decides execution, but under a ContextPolicy what is
    # sent is still the profile, so a tool the policy permits and the profile does
    # not name is invisible and refused as an unknown tool.
    tool_profile: tuple[str, ...] = ()
    permission_policy: PermissionChecker | None = None
    # FR-27 / FR-28. None leaves the model client's own default in place. A
    # value set on the RunConfig overrides the agent's.
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort | str | None = None
    # FR-137 (M38): where the agent's commands run. None gives it no workspace and none of the
    # workspace tools, and leaves its agent_spec_hash as it was (NFR-41).
    workspace: WorkspaceSpec | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_profile", tuple(self.tool_profile))
        if self.workspace is not None and not isinstance(self.workspace, WorkspaceSpec):
            raise ValueError(f"AgentSpec.workspace must be a WorkspaceSpec, got {type(self.workspace).__name__}")
        _refuse_bad_output_limit(self.max_output_tokens, "AgentSpec")
        object.__setattr__(
            self, "reasoning_effort", _reasoning_effort(self.reasoning_effort, "AgentSpec")
        )

    def checker(self) -> PermissionChecker:
        """Default policy: allow exactly the declared tool profile.

        A spec with no explicit policy and no profile therefore permits nothing,
        which is the right default for a permission layer -- an empty allowlist
        denies, it does not wave everything through.
        """
        if self.permission_policy is not None:
            return self.permission_policy
        return AllowlistPermissionChecker(set(self.tool_profile))


def _decimal_text(value: Any) -> str | None:
    """A Decimal as text: JSONB has no decimal, and a float would drift."""
    return None if value is None else str(value)


@dataclass(frozen=True)
class RunConfig:
    tenant_id: str
    project_id: str
    max_turns: int = 10
    model_override: str | None = None
    principal_context: PrincipalContext | None = None
    # The run that spawned this one (FR-21). None for a top-level run, which
    # is the common case. Phase 2's subagents are what populate it; the column
    # and the tenancy rule exist now so that phase adds a caller rather than a
    # migration to a table that already holds production rows.
    parent_run_id: str | uuid.UUID | None = None
    # FR-27 / FR-28: override the agent's values for this run only.
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort | str | None = None
    # FR-43: this run's per-run and per-tool limits, replacing the Runner's as a
    # whole. None runs under the Runner's.
    scheduler_limits: SchedulerLimits | None = None
    # FR-67, FR-68: this run's claim on a plan node's reservation, handed over by
    # whatever drives the plan -- the orchestrator M19 brings. Application code
    # driving a plain run leaves it None; budgets for plain single-agent runs are
    # out of scope (backlog I-04, DECISION-c274eb02).
    budget_lease: BudgetLease | None = None
    # FR-70 to FR-72 (M18): how deep this run sits under its top-level parent, and
    # the schema its answer must match. Both are set by the SubagentPool when it
    # spawns a child, never by application code, as budget_lease is
    # (DECISION-35f4c3f4). None for a run the pool did not place: every top-level
    # run, the orchestrator's depth 0 (P2-D21), and an FR-21 child built by
    # application code (M18a).
    depth: int | None = None
    output_schema: dict[str, Any] | None = None
    # FR-83 (M18a): each input the child was briefed with, as its uri and its
    # provenance, for the request's provenance manifest. Set by the pool.
    briefed_inputs: tuple[tuple[str, ContentProvenance], ...] = ()
    # FR-76 (M19): the plan node this run executes, set by the pool, so its tools'
    # scope and the artifacts they write name it. None outside a plan.
    node_id: str | None = None
    # M19, round 1 D1: why this run ends failed unless something it runs resolves it
    # first. Set by Orchestrator.config(), so an orchestrator run that never completes
    # a plan version -- whose model gives up, or never calls run_plan -- cannot end
    # completed (FR-75). None for every other run.
    pending_failure: str | None = None
    # FR-77, FR-78 (M20): what this run's agent sees -- only the tools its profile may
    # execute -- and when its history is compacted. Set by Orchestrator.config() and
    # carried by the SubagentPool to every child; None sends every registered tool and
    # never compacts, exactly as before M20 (DECISION-468e2bfa, NFR-12).
    context_policy: ContextPolicy | None = None
    # FR-90, FR-92 (M22): the top-level run whose SESSION-scoped sources this run may
    # be served. Set by the SubagentPool on every child; None for a top-level run,
    # whose own id it then is.
    ledger_run_id: str | None = None
    # FR-101 (M26): the gate this run's plan nodes pass, recorded in its manifest. Set
    # by Orchestrator.config(); never carried to a child.
    gate_policy: GatePolicy | None = None
    # FR-116 (M30): how long a call requiring approval waits before it expires, in seconds.
    approval_timeout: float = 3600

    def __post_init__(self) -> None:
        # Before the range checks: a bool passes every one of them (True >= 1,
        # True <= the ceiling) and then fails the write as a SQL boolean.
        if isinstance(self.max_turns, bool):
            raise ValueError("max_turns must be an int, not a bool")
        if self.max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        # And bounded above, because runs.max_turns is an INTEGER column.
        # Refused HERE rather than at the write so the run fails the same way
        # with and without persistence: 2**31 is an ordinary Python int that
        # passed the lower bound, completed in memory, and failed against
        # Postgres with NumericValueOutOfRange (M5 round 8). A ceiling this
        # high is not a real constraint on anyone -- it is the point at which
        # "more turns" stops being a number the store can hold.
        if self.max_turns > _MAX_TURNS_CEILING:
            raise ValueError(
                f"max_turns must be at most {_MAX_TURNS_CEILING} "
                "(runs.max_turns is an INTEGER column)"
            )
        if not self.tenant_id or not self.project_id:
            raise ValueError("tenant_id and project_id are mandatory on every run (ADR-11)")
        # Refused HERE, by name, like max_turns above -- not only by the store.
        # M7 round 1 found RunConfig constructing happily around "not-a-uuid",
        # a NUL and an int, while every sibling field refused at construction:
        # M5 round 8's shape exactly, a caller-supplied value reaching a typed
        # column with no guard at the boundary the caller actually touches.
        # One implementation shared with the store, so the two cannot disagree.
        reason = column_rejection_reason(self.parent_run_id, "UUID")
        if reason is not None:
            raise ValueError(f"parent_run_id cannot be stored: {reason}")
        if isinstance(self.parent_run_id, uuid.UUID):
            # One type on the config, whichever form the caller had to hand.
            object.__setattr__(self, "parent_run_id", str(self.parent_run_id))
        _refuse_bad_output_limit(self.max_output_tokens, "RunConfig")
        object.__setattr__(
            self, "reasoning_effort", _reasoning_effort(self.reasoning_effort, "RunConfig")
        )
        _refuse_bad_scheduler_limits(self.scheduler_limits, "RunConfig")
        # FR-79 (M18a): refused here, by name, as max_turns is -- round 4 found both
        # accepted, and a non-mapping schema surfacing only as a re-ask and a failed
        # run. A depth that is set is the pool placing a child: 1 or more, never a
        # bool (DECISION-53ee27f6 as revised).
        if self.depth is not None and (type(self.depth) is not int or not 1 <= self.depth <= _MAX_TURNS_CEILING):
            raise ValueError(f"depth must be None or an int from 1 to {_MAX_TURNS_CEILING}, got {self.depth!r}")
        if self.output_schema is not None:
            if not isinstance(self.output_schema, Mapping):
                raise ValueError(
                    f"output_schema must be a mapping or None, got {type(self.output_schema).__name__}"
                )
            object.__setattr__(self, "output_schema", dict(self.output_schema))
        if not isinstance(self.briefed_inputs, (list, tuple)) or not all(
            isinstance(entry, tuple) and len(entry) == 2 and type(entry[0]) is str and entry[0]
            and isinstance(entry[1], ContentProvenance)
            for entry in self.briefed_inputs
        ):
            raise ValueError("briefed_inputs must be a tuple of (uri, ContentProvenance) pairs")
        object.__setattr__(self, "briefed_inputs", tuple(self.briefed_inputs))
        if self.node_id is not None and (
            type(self.node_id) is not str or not self.node_id or unstorable_reason(self.node_id) is not None
        ):
            raise ValueError(f"node_id must be None or a non-empty storable str, got {self.node_id!r}")
        if self.pending_failure is not None and (
            type(self.pending_failure) is not str or not self.pending_failure
            or unstorable_reason(self.pending_failure) is not None
        ):
            raise ValueError(f"pending_failure must be None or a non-empty storable str, got {self.pending_failure!r}")
        if self.ledger_run_id is not None and (
            type(self.ledger_run_id) is not str or column_rejection_reason(self.ledger_run_id, "UUID") is not None
        ):
            raise ValueError(f"ledger_run_id must be None or a UUID as text, got {self.ledger_run_id!r}")
        if self.gate_policy is not None and not isinstance(self.gate_policy, GatePolicy):
            raise ValueError(f"gate_policy must be a GatePolicy or None, got {type(self.gate_policy).__name__}")
        timeout = self.approval_timeout
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"approval_timeout must be a finite number of seconds over 0, got {timeout!r}")
        if self.context_policy is not None and not isinstance(self.context_policy, ContextPolicy):
            raise ValueError(f"context_policy must be a ContextPolicy or None, got {type(self.context_policy).__name__}")
        if self.scheduler_limits is not None and self.scheduler_limits.provider_concurrency_limits:
            # A provider limit is shared by every run of a Runner, so one run
            # cannot set it.
            raise ValueError(
                "RunConfig.scheduler_limits cannot carry provider_concurrency_limits: a provider "
                "limit is shared by every run of a Runner, so it is set on the Runner"
            )


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    output: str | None
    events: tuple[RunEvent, ...] = ()
    usage: Usage = field(default_factory=Usage)
    run_id: str = ""
    # Not in the design's four-field sketch, but a failed run that cannot say
    # why is not debuggable. None on success.
    error: str | None = None
    # What the run cost in USD (FR-30): the sum of its model calls' costs, from
    # the prices in the Runner's ModelRegistry. None when that cannot be known
    # -- a model with no price, or a call nothing could price -- never 0.
    cost_usd: Decimal | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.COMPLETED


@dataclass(frozen=True)
class _OpenedRun:
    """Everything a run needs that is decided before it starts."""

    scope: RunScope
    events: EventSink
    client_key: str
    model_id: str | None
    recorded_model: str | None
    max_output_tokens: int | None
    reasoning_effort: ReasoningEffort | None
    meter: RunMeter
    # The provider name the model client declares, read once per run (FR-57).
    provider_name: str | None = None


def _run_timing(control: RunControl) -> dict[str, Any]:
    """FR-57: the run's start and duration, for its terminal event."""
    if control.started_ns is None:
        return {"started_at": None, "duration_ms": 0.0}
    return {"started_at": control.started_at, "duration_ms": elapsed_ms(control.started_ns)}


class Runner:
    def __init__(
        self,
        model_clients: dict[str, ModelClient],
        *,
        session_store: SessionStore | None = None,
        tools: list[Tool] | None = None,
        tool_registry: ToolRegistry | None = None,
        hook: RuntimeHook | None = None,
        assembler: ContextAssembler | None = None,
        persistence: Persistence | None = None,
        model_registry: ModelRegistry | None = None,
        scheduler_limits: SchedulerLimits | None = None,
        mcp_servers: Sequence[Any] | None = None,
        workspace_provider: WorkspaceProvider | None = None,
        max_concurrent_workspaces: int = 4,
    ) -> None:
        if not model_clients:
            raise ValueError("Runner requires at least one model client")
        # Optional: with no persistence the Runner behaves exactly as before,
        # entirely in memory. Phase 0 must stay runnable without a database.
        self._persistence = persistence
        self._models = model_registry if model_registry is not None else default_registry()
        self._clients = dict(model_clients)
        # FR-43: the provider limits every run of this Runner shares, and the
        # per-run limits a run uses unless its RunConfig sets its own.
        _refuse_bad_scheduler_limits(scheduler_limits, "Runner")
        self._limits = scheduler_limits if scheduler_limits is not None else SchedulerLimits()
        unknown = sorted(set(self._limits.provider_concurrency_limits) - set(self._clients))
        if unknown:
            raise ValueError(
                f"provider_concurrency_limits names {unknown}, which are not model clients of this "
                f"Runner; registered: {sorted(self._clients)}"
            )
        self._provider_slots = ProviderSlots(self._limits.provider_concurrency_limits)
        self._sessions = session_store if session_store is not None else InMemorySessionStore()
        # `or` would be wrong here: ToolRegistry defines __len__, so an EMPTY
        # caller-supplied registry is falsy and would be silently discarded and
        # replaced with a fresh one. Identity, not truthiness.
        self._registry = tool_registry if tool_registry is not None else ToolRegistry()
        for tool in tools or ():
            self._registry.register(tool)
        # FR-123 (M33): MCP servers, configured in code. The mcp library is imported only
        # here, when there are servers (NFR-34); None means no MCP at all (NFR-38).
        self._mcp = None
        # FR-127 (M34): the in-memory catalog of a Runner without persistence, made now so the MCP
        # clients can be handed it rather than this Runner: a reference back to the Runner from
        # its own engine would keep a collected Runner alive, and its servers with it (NFR-39).
        self._memory_catalog: Any = None
        if persistence is None:
            from .catalog import InMemoryCatalogStore

            self._memory_catalog = InMemoryCatalogStore()
        if mcp_servers:
            from .mcp import McpClients

            catalog = (persistence.catalog_store if persistence is not None
                       else (lambda store=self._memory_catalog: store))
            self._mcp = McpClients(list(mcp_servers), frozenset(self._registry.names()), catalog=catalog)
        # FR-138 (M38): the workspace provider, which only the WorkspaceManager calls. A registered
        # tool runs on the host, so none may take a workspace tool's name beside a provider: an
        # agent's run_command would run outside its sandbox (M38 pre-flight).
        if (isinstance(max_concurrent_workspaces, bool) or not isinstance(max_concurrent_workspaces, int)
                or max_concurrent_workspaces < 1):
            raise ValueError(f"max_concurrent_workspaces must be a whole number of at least 1, "
                             f"got {max_concurrent_workspaces!r}")
        self._memory_workspaces: Any = None
        if persistence is None:
            from .workspace.records import InMemoryWorkspaceStore

            self._memory_workspaces = InMemoryWorkspaceStore()
        self._workspaces: Any = None
        self._workspace_runs: dict[str, Any] = {}  # run id -> its RunWorkspace, while the run is under way
        if workspace_provider is not None:
            from .workspace.manager import WorkspaceManager
            from .workspace.tools import RESERVED

            if not isinstance(workspace_provider, WorkspaceProvider):
                raise ValueError(f"workspace_provider must be a WorkspaceProvider, got {type(workspace_provider).__name__}")
            taken = sorted(set(RESERVED) & set(self._registry.names()))
            if taken:
                raise ValueError(f"registered tools take workspace tool names: {taken}")
            self._workspaces = WorkspaceManager(workspace_provider, self._workspace_store, max_concurrent_workspaces)
        self._hook = hook if hook is not None else RuntimeHook()
        self._assembler = assembler if assembler is not None else ContextAssembler()
        # FR-76 (M19): with no persistence, the in-memory artifact and run-state
        # stores a run's tools reach, admitting exactly the runs this Runner started.
        self._started: set[tuple[str, str, str]] = set()
        # FR-100 (M26): each started run's parent, so the in-memory ledger store can tell
        # whether a node's run belongs to a ledger, as Postgres reads runs.parent_run_id.
        self._parents: dict[tuple[str, str, str], str | None] = {}
        self._memory_artifacts: InMemoryArtifactStore | None = None
        self._memory_states: InMemoryRunStateStore | None = None
        self._memory_evidence: InMemoryEvidenceStore | None = None
        self._memory_ledger: InMemoryLedgerStore | None = None
        self._memory_interruptions: InMemoryInterruptionStore | None = None
        # FR-115 (M30): the interruptions this Runner's runs are waiting on, in this process.
        self._approvals = ApprovalWaits()
        # FR-120 (M32): every run of this Runner under way in this process -- its event sink,
        # control and configuration -- so a child's approval is asked on its top-level
        # run's stream too, and a child inherits its parent's approval_timeout.
        self._live: dict[str, tuple[Any, RunControl, RunConfig]] = {}
        # FR-95 (M24): one claim-lock per Runner, across its runs, and across processes
        # through the database when there is one.
        self._claim_lock = ClaimLock(persistence.dsn if persistence is not None else None)

    def _admits(self, tenant_id: str, project_id: str, run_id: str) -> bool:
        return (tenant_id, project_id, run_id) in self._started

    def _artifacts_for(self, tenant_id: str, project_id: str) -> Any:
        """The artifact store bound to a run's tenant and project (FR-76)."""
        if self._persistence is not None:
            return self._persistence.artifact_store(tenant_id, project_id)
        if self._memory_artifacts is None:
            self._memory_artifacts = InMemoryArtifactStore(tenant_id, project_id, runs=self._admits)
        return self._memory_artifacts.for_scope(tenant_id, project_id)

    def _evidence_store(self) -> Any:
        """Where this Runner's source versions are kept (FR-90): the database, or one
        in-memory store for the Runner's life."""
        if self._persistence is not None:
            return self._persistence.evidence_store()
        if self._memory_evidence is None:
            self._memory_evidence = InMemoryEvidenceStore()
        return self._memory_evidence

    def _ledger_store(self) -> Any:
        """Where this Runner's evidence ledgers are kept (FR-92)."""
        if self._persistence is not None:
            return self._persistence.ledger_store()
        if self._memory_ledger is None:
            self._memory_ledger = InMemoryLedgerStore(self._evidence_store(), nodes=self._node_of_ledger)
        return self._memory_ledger

    def _interruption_store(self) -> Any:
        """Where this Runner's interruptions and decisions are kept (FR-112): the database,
        or one in-memory store for the Runner's life."""
        if self._persistence is not None:
            return self._persistence.interruption_store()
        if self._memory_interruptions is None:
            self._memory_interruptions = InMemoryInterruptionStore(
                runs=self._admits, parent=lambda tenant, project, run: self._parents.get((tenant, project, run)))
        return self._memory_interruptions

    def _workspace_store(self) -> Any:
        """Where this Runner's workspaces and commands are recorded (FR-140): the database, or one
        in-memory store for the Runner's life."""
        if self._persistence is not None:
            return self._persistence.workspace_store()
        return self._memory_workspaces

    def _workspace_refusal(self, spec: WorkspaceSpec) -> str:
        """FR-138: why a run whose agent has a workspace was given none."""
        if self._workspaces is None:
            return "this Runner was given no workspace_provider"
        if spec.scope != "run":
            return f"WorkspaceSpec.scope {spec.scope!r} is not supported yet; only 'run' is"
        return "WorkspaceSpec.keep=True is not supported yet"

    async def _end_workspace(self, run_id: str, events: EventSink, control: RunControl) -> None:
        """FR-140: a run's workspace is removed, and recorded so, before the run's terminal event,
        whatever its status. Never raises."""
        workspace = self._workspace_runs.pop(str(run_id), None)
        if workspace is not None:
            await workspace.end("run_ended", partial(self._safe_emit_kind, events, control))

    async def _safe_emit_kind(self, events: EventSink, control: RunControl, event_type: EventType,
                              payload: dict) -> None:
        await self._safe_emit(events, event_type, payload, control)

    def _catalog_store(self) -> Any:
        """Where this Runner's MCP catalog is kept (FR-127): the database, or one in-memory store
        for the Runner's life."""
        if self._persistence is not None:
            return self._persistence.catalog_store()
        return self._memory_catalog

    async def accept_tools(self, tenant_id: str, project_id: str, alias: str, tool_hashes: Sequence[str], *,
                           principal: PrincipalContext) -> list[Any]:
        """FR-127: accepts tool versions -- by their `tool_hash`, as `McpToolBlocked` names them --
        for one tenant and project, and returns the acceptance rows, each in the order given.

        Refused with nothing recorded, in this order: a principal naming no user or service,
        ValueError; an alias this Runner was not given, UnknownMcpServer; a hash with no version
        row in that tenancy, UnknownToolVersion, exactly as for one in another tenancy. A hash
        already accepted is returned unchanged. Like `decide`, it is never a tool. A tool becomes
        usable at its connection's next read of the catalog: the next turn of a run, or the next
        check before a call."""
        from .catalog import _decider

        _decider(principal)
        if self._mcp is None or alias not in self._mcp.servers:
            raise UnknownMcpServer(f"no MCP server {alias!r} was given to this Runner")
        if isinstance(tool_hashes, str):
            raise ValueError("tool_hashes is a list of hashes, not one string")
        return await asyncio.to_thread(self._catalog_store().accept, tenant_id, project_id, alias,
                                       list(tool_hashes), principal)

    async def mcp_blocked(self, tenant_id: str, project_id: str, alias: str) -> tuple[Any, ...]:
        """FR-127 (owner, M37 pre-flight): through this tenancy's connection, an `McpBlocked` of
        kind `tool` for each tool the server lists now whose current version is not accepted --
        its hash, and the last accepted one with the parts that differ, or none on first sight --
        so an application can accept a version any run of the tenancy met, a child's included. A
        stale listing is read again first. Refused as `mcp_resources` is; never a tool."""
        self._mcp_tenancy(tenant_id, project_id, alias)
        return await self._mcp.application(alias, tenant_id, project_id, "blocked")

    def _mcp_tenancy(self, tenant_id: Any, project_id: Any, alias: Any) -> None:
        """FR-131: the tenancy whose connection an application read uses, and a server this
        Runner was given."""
        from .primitives import unstorable_reason

        for name, value in (("tenant_id", tenant_id), ("project_id", project_id)):
            if not isinstance(value, str) or not value or unstorable_reason(value) is not None:
                raise ValueError(f"{name} must be non-empty storable text")
        if self._mcp is None or alias not in self._mcp.servers:
            raise UnknownMcpServer(f"no MCP server {alias!r} was given to this Runner")

    async def mcp_resources(self, tenant_id: str, project_id: str, alias: str) -> Any:
        """FR-131: the resources a server lists for this tenancy, the resource templates accepted
        in their current version, and the blocked ones (`McpResources`). Everything in it is the
        server's, and untrusted. Never a tool: the application chooses what to read."""
        self._mcp_tenancy(tenant_id, project_id, alias)
        return await self._mcp.application(alias, tenant_id, project_id, "resources")

    async def mcp_read(self, tenant_id: str, project_id: str, alias: str, uri: str) -> Any:
        """FR-131: reads one resource through this tenancy's connection and stores it as an
        artifact of that tenancy with NFR-35's labels (FR-52); returns its `ArtifactRef`, ready
        to brief a child as an input (FR-83). The store is written on this loop, never the MCP
        loop (FR-81)."""
        from .mcp import _MAX_URI, _UNTRUSTED

        self._mcp_tenancy(tenant_id, project_id, alias)
        from .primitives import unstorable_reason

        if not isinstance(uri, str) or not uri or len(uri) > _MAX_URI or unstorable_reason(uri) is not None:
            raise ValueError(f"uri must be 1 to {_MAX_URI} characters of storable text")
        data, mime = await self._mcp.application(alias, tenant_id, project_id, "read", uri)
        return await self._artifacts_for(tenant_id, project_id).put(
            data, mime_type=mime, provenance=_UNTRUSTED.for_source(f"mcp://{alias}/resources/{uri}"),
            created_by_agent=f"mcp:{alias}")

    async def mcp_prompts(self, tenant_id: str, project_id: str, alias: str) -> Any:
        """FR-131: the prompts a server lists for this tenancy accepted in their current version,
        and the blocked ones (`McpPrompts`); accept them with `accept_tools`, by hash."""
        self._mcp_tenancy(tenant_id, project_id, alias)
        return await self._mcp.application(alias, tenant_id, project_id, "prompts")

    async def mcp_prompt(self, tenant_id: str, project_id: str, alias: str, name: str,
                         arguments: Mapping[str, str] | None = None) -> Any:
        """FR-131: a prompt as an `McpPrompt`, with NFR-35's labels. A prompt not accepted in its
        current version, or not listed, is refused `ToolBlocked`. An application that uses it as
        a task does so explicitly."""
        from .primitives import unstorable_reason

        self._mcp_tenancy(tenant_id, project_id, alias)
        arguments = dict(arguments or {})
        if not isinstance(name, str) or not name or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in arguments.items()) \
                or unstorable_reason([name, arguments]) is not None:
            raise ValueError("a prompt is named by text, and its arguments map text to text")
        return await self._mcp.application(alias, tenant_id, project_id, "prompt", name, arguments)

    def _node_of_ledger(self, tenant_id: str, project_id: str, ledger_run_id: str, subject: Any) -> bool:
        """FR-100, in memory: the node is stored for its run in this tenant and project, and
        that run is the ledger's run or descends from it -- the rule the Postgres store
        applies to plan_node_states and runs.parent_run_id."""
        run, seen = subject.run_id, set()
        while run is not None and run != ledger_run_id and run not in seen:
            seen.add(run)
            run = self._parents.get((tenant_id, project_id, run))
        if run != ledger_run_id or (tenant_id, project_id, subject.run_id) not in self._started:
            return False
        states = self._memory_states
        if states is None:
            return False
        with states._lock:
            plan = states._plans.get((tenant_id, project_id, subject.plan_id, subject.version))
        return plan is not None and plan.run_id == subject.run_id and any(
            node.node_id == subject.node_id for node in plan.nodes)

    async def record_validation(
        self, tenant_id: str, project_id: str, ledger_run_id: str, subject: str | NodeSubject, source: str,
        verdict: str, detail: str,
    ) -> EvidenceVerification:
        """FR-103: an externally validated outcome -- a test, a known-answer fixture, a
        downstream outcome or a human adjudication -- recorded for a claim (by its uri) or
        a plan node of the ledger. A Runner method in no tool registry, which no tool the
        SDK ships calls, so no model can record an outcome about its own work; a custom
        tool is first-party code and reaches the Runner through its scope (FR-110,
        declared). It changes no claim's provenance (P3-D15). A subject outside the ledger's tenant, project or ledger is refused with
        LookupError and nothing is recorded."""
        from datetime import datetime, timezone

        row = EvidenceVerification(
            verification_id=str(uuid.uuid4()), ledger_run_id=ledger_run_id, subject=subject, kind="external",
            outcome=verdict,
            detail={"source": source, "detail": detail},
            provenance=ContentProvenance.internal_tool(f"urn:agentsdk:validation:{source}"),
            tenant_id=tenant_id, project_id=project_id, created_at=datetime.now(timezone.utc),
        )
        await asyncio.to_thread(self._ledger_store().append_verifications, [row])
        return row

    async def calibration_report(
        self, tenant_id: str, project_id: str, *, since: datetime | None = None, min_outcomes: int = 20,
    ) -> CalibrationReport:
        """FR-107: the gate's signals and thresholds against external outcomes, read from
        one tenant and project's gate, critic and external verifications on a worker
        thread. It changes nothing: its suggested threshold is for a person to apply or
        not (P3-D19)."""
        _checked(since, min_outcomes)
        rows = await asyncio.to_thread(self._ledger_store().verifications_since, tenant_id, project_id, since,
                                       kinds=("gate", "critic", "external"))
        return calibrate(rows, tenant_id=tenant_id, project_id=project_id, since=since, min_outcomes=min_outcomes)

    async def decide(
        self, tenant_id: str, project_id: str, interruption_id: str, *, approve: bool, principal: PrincipalContext,
        message: str | None = None, sticky: bool = False,
    ) -> InterruptionDecision:
        """FR-115: records a decision about a call waiting for approval, and wakes its run.

        `principal` must name a user or a service: the agent alone may not decide.
        `sticky` also records a standing decision for the interruption's run, agent and
        tool, which decides that run's not yet decided calls the tool's policy flagged
        (P4-D15). Refused with nothing recorded, tenancy first, so another tenant's
        decided interruption reads as unknown: UnknownInterruption, then
        InterruptionAlreadyDecided, InterruptionExpired, InterruptionNotWaiting. The
        store's unique key settles a race: of two decisions, or a decision and the run's
        own expiry, exactly one is recorded. A Runner method in no tool registry, which
        no tool the SDK ships calls (AC-83). May be awaited on any loop or thread."""
        from datetime import datetime, timezone

        if type(approve) is not bool:
            raise ValueError(f"approve must be a bool, got {approve!r}")
        if type(sticky) is not bool:
            raise ValueError(f"sticky must be a bool, got {sticky!r}")
        made = InterruptionDecision(
            decision_id=str(uuid.uuid4()), interruption_id=str(uuid.uuid4()), tenant_id=tenant_id,
            project_id=project_id, decision="approve" if approve else "reject", source="principal",
            message=message, principal=principal, decided_at=datetime.now(timezone.utc))
        store = self._interruption_store()
        view = await asyncio.to_thread(store.interruption, tenant_id, project_id, interruption_id)
        # FR-133 (M36): second, after tenancy -- an input request is answered with respond.
        from .outcomes import InterruptionKind

        if view.interruption.kind is not InterruptionKind.TOOL_APPROVAL:
            raise InterruptionKindMismatch(f"interruption {interruption_id} is an input request: answer it with respond")
        if view.decision is not None:
            raise InterruptionAlreadyDecided(f"interruption {interruption_id} is already decided")
        item = view.interruption
        if datetime.now(timezone.utc) >= item.expires_at:
            raise InterruptionExpired(f"interruption {interruption_id} expired at {item.expires_at.isoformat()}")
        if not self._approvals.waiting(item.interruption_id):
            raise InterruptionNotWaiting(f"no run of this Runner is waiting on interruption {interruption_id}")
        made = replace(made, interruption_id=item.interruption_id, decided_at=datetime.now(timezone.utc))
        await asyncio.to_thread(store.append_decision, made)
        # The run is woken once its decision is recorded, whatever happens to a sticky
        # decision after it (M30 review caveat 4).
        self._approvals.wake(item.interruption_id)
        if sticky:
            try:
                await asyncio.to_thread(store.append_standing, StandingDecision(
                    standing_id=str(uuid.uuid4()), run_id=item.run_id, tenant_id=tenant_id, project_id=project_id,
                    agent_id=item.agent_id, tool_name=item.tool_name, decision=made.decision, principal=principal,
                    message=message, created_at=datetime.now(timezone.utc),
                    # FR-128 (M34): bound to the version the request was for (NFR-36).
                    tool_hash=item.request_payload.get("tool_hash")))
            except Exception as exc:
                raise RuntimeError(
                    f"the decision on interruption {interruption_id} was recorded, but its standing decision was "
                    f"not: {describe_exception(exc)}") from exc
            self._approvals.wake(item.interruption_id)
        return made

    async def respond(
        self, tenant_id: str, project_id: str, interruption_id: str, *, action: str, content: Any = None,
        principal: PrincipalContext,
    ) -> InterruptionDecision:
        """FR-133: answers an MCP server's request for more input -- `accept`, `decline` or
        `cancel` -- and wakes the call waiting on it, which returns the answer to the server.

        `principal` must name a user or a service. In form mode `accept` takes `content`, which
        must match the schema the server asked for, checked in a process of its own since the
        schema is the server's; in url mode `accept` means the person completed the flow
        themselves, and the SDK opens nothing. No other answer takes content. Refused with nothing
        recorded, in decide's order with the kind second: UnknownInterruption,
        InterruptionKindMismatch, InterruptionAlreadyDecided, InterruptionExpired,
        InterruptionNotWaiting. A Runner method in no tool registry; may be awaited on any loop."""
        from datetime import datetime, timezone

        from .interruptions import ANSWERS, INPUT_KINDS

        if action not in ANSWERS:
            raise ValueError(f"action must be one of {', '.join(ANSWERS)}, got {action!r}")
        made = InterruptionDecision(
            decision_id=str(uuid.uuid4()), interruption_id=str(uuid.uuid4()), tenant_id=tenant_id,
            project_id=project_id, decision=action, source="principal", principal=principal,
            decided_at=datetime.now(timezone.utc))
        store = self._interruption_store()
        view = await asyncio.to_thread(store.interruption, tenant_id, project_id, interruption_id)
        item = view.interruption
        if item.kind not in INPUT_KINDS:
            raise InterruptionKindMismatch(f"interruption {interruption_id} is an approval: decide it with decide")
        if view.decision is not None:
            raise InterruptionAlreadyDecided(f"interruption {interruption_id} is already decided")
        if datetime.now(timezone.utc) >= item.expires_at:
            raise InterruptionExpired(f"interruption {interruption_id} expired at {item.expires_at.isoformat()}")
        if not self._approvals.waiting(item.interruption_id):
            raise InterruptionNotWaiting(f"no run of this Runner is waiting on interruption {interruption_id}")
        response = None
        form = item.request_payload.get("mode") == "form"
        if action == "accept" and form:
            if not isinstance(content, dict):
                raise ValueError("accepting a form takes its content, a JSON object")
            from .mcp import validate_untrusted

            invalid, unreadable = await asyncio.to_thread(
                validate_untrusted, content, item.request_payload.get("requested_schema"))
            if invalid is not None or unreadable is not None:
                raise ValueError(f"the content does not match the requested schema: {invalid or unreadable}")
            from .mcp import answer_problem

            problem = answer_problem(content)
            if problem is not None:  # pre-review D3: recorded only if it can reach the server
                raise ValueError(f"the content cannot be answered: {problem}")
            response = content
        elif content is not None:
            raise ValueError("content is given only to accept a form")
        made = replace(made, interruption_id=item.interruption_id, decided_at=datetime.now(timezone.utc),
                       response=response)
        await asyncio.to_thread(store.append_decision, made)
        self._approvals.wake(item.interruption_id)
        return made

    async def interruptions(
        self, tenant_id: str, project_id: str, *, run_id: str | None = None, pending_only: bool = True,
    ) -> list[InterruptionView]:
        """FR-115: the interruptions of one tenant and project -- with `run_id`, those of
        that run and every run beneath it -- through the store on a worker thread, each
        marked `waiting` when a run of this Runner waits on it (P4-D14)."""
        views = await asyncio.to_thread(
            self._interruption_store().interruptions, tenant_id, project_id, run_id=run_id, pending_only=pending_only)
        return [replace(v, waiting=self._approvals.waiting(v.interruption.interruption_id)) for v in views]

    def evidence_ledger(self, tenant_id: str, project_id: str, ledger_run_id: str) -> EvidenceLedger:
        """FR-94: the ledger of a top-level run, read within its tenant and project.
        Its methods are store calls: from a coroutine, call them on a worker thread."""
        return EvidenceLedger(self._ledger_store(), tenant_id, project_id, ledger_run_id)

    def _run_states_for(self, tenant_id: str, project_id: str) -> Any:
        """The RunStateStore for a run's tenant and project (FR-65, M19)."""
        if self._persistence is not None:
            return self._persistence.run_state_store(tenant_id, project_id)
        if self._memory_states is None:
            self._memory_states = InMemoryRunStateStore(tenant_id, project_id, runs=self._admits)
        return self._memory_states.for_scope(tenant_id, project_id)

    async def aclose(self) -> None:
        """FR-124 (M33): closes this Runner's MCP connections, whichever loop their runs were on,
        ending their servers and the servers' children; none opens after (NFR-39)."""
        if self._mcp is not None:
            await self._mcp.aclose()

    async def __aenter__(self) -> Runner:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def run(self, spec: AgentSpec, task: str, config: RunConfig) -> RunResult:
        """Drive one agent to a terminal status (FR-1): a run started with start()
        and awaited to its end (FR-48).

        TOTAL boundary. FR-1 promises a terminal status, and a promise honoured
        only for the failures someone remembered to enumerate is not a promise:
        that is the mistake the ModelClient boundary already made three times.
        A hook that raises, a session store that loses its connection, a
        malformed tool schema -- all become RunStatus.FAILED with a reason,
        never an exception reaching application code.

        Configuration errors are the deliberate exception. An unknown model
        client, an invalid RunConfig, or a reasoning effort with no output limit
        is a caller bug that must surface loudly at the call site, not be buried
        in a failed RunResult the caller might not inspect. Those raise before
        the run is considered started.

        Cancellation is recorded, then re-raised (FR-50, amending
        INVARIANT-af776957). Cancelling the task that awaits this, or a
        CancelledError raised by a collaborator, ends the run `cancelled` with its
        usage and cost recorded, and CancelledError then reaches the caller:
        before M12 it passed straight through and left a persisted run `running`
        forever (KNOWLEDGE-b3c2b462). Any other BaseException still passes
        through as control flow.
        """
        handle = await self.start(spec, task, config)
        try:
            result = await handle.result()
        except asyncio.CancelledError:
            handle.cancel()
            await handle._settled()
            raise
        if result.status is RunStatus.CANCELLED:
            raise asyncio.CancelledError()
        return result

    async def start(self, spec: AgentSpec, task: str, config: RunConfig) -> RunHandle:
        """Start a run and return its handle without waiting for it (FR-48).

        Configuration is validated exactly as run() validates it, and the same
        errors raise here, at the call site, before any run is started. The run
        belongs to the event loop that started it.
        """
        opened = self._open(spec, config)
        loop = asyncio.get_running_loop()
        control = RunControl()
        # So a cancel() called from another thread can hand the cancellation to
        # the run's own loop (M12 review round 1, C1).
        control.loop = loop
        handle = RunHandle(opened.scope.run_id, control, opened.meter, runner=self,
                           tenant_id=config.tenant_id, project_id=config.project_id)
        events = PublishingSink(opened.events, handle._publish, loop)
        control.task = asyncio.ensure_future(self._drive(spec, task, config, opened, events, control))
        control.task.add_done_callback(handle._finished)
        return handle

    def _open(self, spec: AgentSpec, config: RunConfig) -> _OpenedRun:
        """Everything decided before a run starts; configuration errors raise here."""
        run_id = str(uuid.uuid4())
        scope = RunScope(run_id=run_id, tenant_id=config.tenant_id, project_id=config.project_id)
        events: EventSink = (
            self._persistence.event_sink_for(scope)
            if self._persistence is not None
            else InMemoryEventSink(config.tenant_id, config.project_id, run_id)
        )
        client_key, model_id = self._resolve_model(spec, config)
        max_output_tokens = (
            config.max_output_tokens
            if config.max_output_tokens is not None
            else spec.max_output_tokens
        )
        reasoning_effort = (
            config.reasoning_effort if config.reasoning_effort is not None else spec.reasoning_effort
        )
        if reasoning_effort is not None and max_output_tokens is None:
            # Decision D2. The SDK cannot see a client's own default output
            # limit, and a provider that budgets reasoning inside it refuses the
            # request: Claude over the gateway answers HTTP 400 when max_tokens
            # does not exceed the thinking budget (KNOWLEDGE-d625552b).
            raise ValueError(
                "reasoning_effort is set but max_output_tokens is not: set "
                "max_output_tokens on the AgentSpec or the RunConfig, large enough "
                "for the reasoning the model may spend as well as its answer"
            )
        # FR-32: a run records the model it used, including a client's own
        # default when nothing was named (KNOWLEDGE-862c2e9e).
        recorded_model = model_id or self._default_model_id(client_key)
        # The run's account, created here so that it outlives any exception the
        # loop raises: the failure path reports what the meter recorded, not what
        # the events happened to capture (R2).
        meter = RunMeter(no_call_cost=self._cost(recorded_model, Usage()))
        # FR-69: a USD ceiling on a model nothing prices is a configuration error, so
        # it is raised here, at the call site, with the rest of them -- not inside the
        # run, where every exception becomes a FAILED result.
        self._budget_record(config.budget_lease, recorded_model)
        # FR-78: so is a compacting policy on a model whose window nothing gives
        # (DECISION-468e2bfa).
        self._context_window(config, recorded_model)
        provider_name = self._provider_name(client_key)
        return _OpenedRun(
            scope=scope,
            events=events,
            client_key=client_key,
            model_id=model_id,
            recorded_model=recorded_model,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            meter=meter,
            provider_name=provider_name,
        )

    async def _drive(
        self,
        spec: AgentSpec,
        task: str,
        config: RunConfig,
        opened: _OpenedRun,
        events: EventSink,
        control: RunControl,
    ) -> RunResult:
        """The run itself, to a terminal status: the total boundary run() documents."""
        control.started = True
        control.started_at, control.started_ns = wall_clock(), now_ns()
        scope, meter = opened.scope, opened.meter
        self._live[scope.run_id] = (events, control, config)
        try:
            return await self._driven(spec, task, config, opened, events, control)
        finally:
            self._live.pop(scope.run_id, None)

    def _top_emitter(self, run_id: str, top_run_id: str, control: RunControl) -> Any:
        """FR-120: for a run beneath another, a way to emit an approval event on its
        top-level run's stream as well as its own; None for a top-level run."""
        if top_run_id == run_id:
            return None

        async def emit_top(event_type: str, payload: dict[str, Any], task_id: str | None) -> None:
            live = self._live.get(top_run_id)
            if live is not None:
                await control.store(live[0].emit, event_type, payload, task_id=task_id)

        return emit_top

    def _approval_timeout_of(self, run_id: str) -> float | None:
        """FR-120: the approval_timeout of a run under way in this process, for its child."""
        live = self._live.get(str(run_id))
        return None if live is None else live[2].approval_timeout

    async def _driven(
        self,
        spec: AgentSpec,
        task: str,
        config: RunConfig,
        opened: _OpenedRun,
        events: EventSink,
        control: RunControl,
    ) -> RunResult:
        """_drive's body, run while the run is registered as live."""
        scope, meter = opened.scope, opened.meter
        try:
            return await self._run(
                spec,
                task,
                config,
                scope,
                events,
                opened.client_key,
                opened.model_id,
                recorded_model=opened.recorded_model,
                max_output_tokens=opened.max_output_tokens,
                reasoning_effort=opened.reasoning_effort,
                meter=meter,
                control=control,
                provider_name=opened.provider_name,
            )
        except asyncio.CancelledError:
            return await self._cancelled(scope, events, meter, control, config.budget_lease)
        except Exception as exc:  # noqa: BLE001
            if control.requested and not control.terminal:
                # Asked to stop first: the run ends as it was asked to, whatever
                # failed on the way out (FR-50).
                return await self._cancelled(scope, events, meter, control, config.budget_lease)
            reason = describe_exception(exc)
            if not control.terminal_written:
                await self._end_workspace(scope.run_id, events, control)
            already_terminal = control.terminal_written
            control.terminal = True
            if not already_terminal:
                await self._safe_emit(
                    events, EventType.RUN_FAILED,
                    {"status": "failed", "reason": reason, **_run_timing(control)}, control,
                )
                control.terminal_written = True
            # Else the run already wrote its terminal event and only the store call
            # after it failed: the caller still hears the failure in the result, but
            # nothing is written after a terminal event (round 4, J1).
            if not already_terminal:
                await self._safe_finish(
                    scope, RunStatus.FAILED, meter.usage, meter.cost_usd, control,
                    self._spend_record(config.budget_lease, meter.usage, meter.cost_usd, meter.budget_tokens),
                    meter.price_table_date,
                )
            # Else the run reached its own ending and only the write after it failed.
            # Trying again here could record FAILED for a run that completed, which is
            # a worse lie than the row the failed write left behind (round 4, J1).
            return RunResult(
                status=RunStatus.FAILED,
                output=None,
                events=events.events(),
                # The meter's totals: every call the model answered, recorded
                # before anything that could fail. Reporting zero, or only the
                # calls whose events were written, would under-report cost on
                # exactly the runs someone is investigating.
                usage=meter.usage,
                run_id=scope.run_id,
                error=reason,
                cost_usd=meter.cost_usd,
            )

    async def _cancelled(
        self,
        scope: RunScope,
        events: EventSink,
        meter: RunMeter,
        control: RunControl,
        lease: Any | None = None,
    ) -> RunResult:
        """FR-50: record the run cancelled, as every terminal path records itself.

        RunCancelled is emitted once and is the run's last event; the row is
        finished `cancelled` with its usage and cost (FR-31). When a model call was
        in flight, that call may be billed and reports no usage, so the cost is
        unknown rather than understated (P2-D7, NFR-11).
        """
        await self._end_workspace(scope.run_id, events, control)
        control.terminal = True
        reason = _cancellation_reason(control.reason)
        cost = None if control.cancelled_in_flight else meter.cost_usd
        await self._safe_emit(
            events,
            EventType.RUN_CANCELLED,
            {"status": RunStatus.CANCELLED.value, "turns": control.turns, "reason": reason, **_run_timing(control)},
            control,
        )
        control.terminal_written = True
        await self._safe_finish(
            scope, RunStatus.CANCELLED, meter.usage, cost, control,
            self._spend_record(lease, meter.usage, cost, meter.budget_tokens), meter.price_table_date,
        )
        return RunResult(
            status=RunStatus.CANCELLED,
            output=None,
            events=events.events(),
            usage=meter.usage,
            run_id=scope.run_id,
            error=reason,
            cost_usd=cost,
        )

    @staticmethod
    def _spend_record(
        lease: Any, usage: Usage, cost_usd: Decimal | None, budget_tokens: int | None = None
    ) -> dict[str, Any] | None:
        """FR-68, NFR-23: what THIS run spent, for its manifest. None without a lease.

        Round 1 recorded the lease's running total here, so a second run on the same
        lease inherited the first one's spend and a run refused before its first call
        recorded a spend it never made (F1). The run's own meter is what FR-68 means by
        its final spend and what NFR-23 compares with its ModelCalled costs; the node's
        total to date is kept beside it, under its own key.
        """
        if lease is None:
            return None
        try:
            return Runner._spend_fields(lease, usage, cost_usd, budget_tokens)
        except Exception:  # noqa: BLE001 - accounting never fails a run (NFR-11, H1)
            return {"usd": None, "tokens": None, "node_id": None, "node_total": None}

    @staticmethod
    def _spend_fields(
        lease: Any, usage: Usage, cost_usd: Decimal | None, budget_tokens: int | None
    ) -> dict[str, Any]:
        node = lease.spent
        return {
            "usd": _decimal_text(cost_usd),
            # The sum of what each call counted, exactly as the lease charged them:
            # a run's summed usage loses the calls whose provider reported no total
            # (round 3, H2), and one call's count is not a run's (round 2, G1).
            "tokens": tokens_of(usage) if budget_tokens is None else budget_tokens,
            "node_id": lease.node_id,
            "node_total": {"usd": _decimal_text(node.usd), "tokens": node.tokens},
        }

    async def _safe_finish(
        self,
        scope: RunScope,
        status: RunStatus,
        usage: Usage,
        cost_usd: Decimal | None,
        control: RunControl,
        budget_spend: dict[str, Any] | None = None,
        price_table_date: str | None = None,
    ) -> None:
        # Threaded like every other store call (FR-20). This one runs once, on
        # a terminal path that is already failing or cancelled, so it is not what
        # NFR-8 measures -- but a store call that blocks the loop only when a run
        # is already failing is the kind of inconsistency that gets read as an
        # oversight later.
        if self._persistence is None:
            return
        try:
            await control.store(self._finish, scope, status, usage, cost_usd, budget_spend, price_table_date)
        except Exception:  # noqa: BLE001 - persistence must not mask the real outcome
            pass

    def _finish(
        self,
        scope: RunScope,
        status: RunStatus,
        usage: Usage,
        cost_usd: Decimal | None,
        budget_spend: dict[str, Any] | None = None,
        price_table_date: str | None = None,
    ) -> None:
        """The terminal write, carrying the run's usage and cost to a recorder
        that declares it records them (FR-31).

        Declared, not inferred. Round 1 offered accounting to any finish_run
        whose signature could bind it, and a pre-M9 recorder wrapped without
        functools.wraps -- which looks like (*args, **kwargs) -- took the
        arguments, raised, and turned a completed run FAILED with both terminal
        events emitted. A recorder that declares nothing is called exactly as
        it was before M9.
        """
        runs = self._persistence.runs
        if getattr(runs, "records_accounting", False) is True:
            # The budget is offered the same way M9's accounting is: only to a
            # recorder that declares it takes it, never inferred from a signature.
            if (budget_spend is not None or price_table_date is not None) and getattr(
                runs, "records_budget", False
            ) is True:
                runs.finish_run(
                    scope, status.value, usage=usage, cost_usd=cost_usd,
                    budget_spend=budget_spend, price_table_date=price_table_date,
                )
            else:
                runs.finish_run(scope, status.value, usage=usage, cost_usd=cost_usd)
        else:
            runs.finish_run(scope, status.value)

    @staticmethod
    async def _safe_emit(events: EventSink, event_type: EventType, payload: dict, control: RunControl) -> None:
        """Telemetry must not be able to fail a terminal path."""
        try:
            await control.store(events.emit, event_type, payload)
        except Exception:  # noqa: BLE001
            pass

    async def _run(
        self,
        spec: AgentSpec,
        task: str,
        config: RunConfig,
        scope: RunScope,
        events: EventSink,
        client_key: str,
        model_id: str | None,
        *,
        recorded_model: str | None,
        max_output_tokens: int | None,
        reasoning_effort: ReasoningEffort | None,
        meter: RunMeter,
        control: RunControl,
        provider_name: str | None = None,
    ) -> RunResult:
        run_id = scope.run_id
        self._started.add((scope.tenant_id, scope.project_id, run_id))
        self._parents[(scope.tenant_id, scope.project_id, run_id)] = (
            None if config.parent_run_id is None else str(config.parent_run_id))
        control.failure = config.pending_failure
        limits = self._limits_for(config)
        # FR-69: refused before anything runs, with or without persistence.
        budget_record = self._budget_record(config.budget_lease, recorded_model)
        # FR-77 (M20): the tools this run's agent is sent, and so the only ones its
        # executor resolves. Every registered tool when the run carries no policy.
        policy = config.context_policy
        registry = self._registry if policy is None else policy.visible(self._registry, spec.tool_profile)
        # FR-146 (M38): plus the workspace tools its agent names, bound to this run's workspace,
        # which is created at their first need (FR-138). None for a run without one.
        if (spec.workspace is not None and self._workspaces is not None and spec.workspace.scope == "run"
                and not spec.workspace.keep):
            from .workspace.tools import NAMES, workspace_tools

            workspace = self._workspaces.for_run(spec.workspace, scope.tenant_id, scope.project_id, run_id,
                                                 partial(control.store, events.emit), control.store)
            self._workspace_runs[run_id] = workspace
            extended = ToolRegistry()
            for name in registry.names():
                extended.register(registry.get(name))
            for tool in workspace_tools(workspace, spec.workspace, [n for n in NAMES if n in spec.tool_profile]):
                extended.register(tool)
            registry = extended
        # FR-125 (M33): plus the MCP tools its agent names, rebuilt each turn; read once
        # here so the manifest records the tools usable at start.
        if self._mcp is not None:
            registry = self._mcp.view(registry, spec.tool_profile, config.tenant_id, config.project_id)
            await registry.prepare()
        window = self._context_window(config, recorded_model)
        sessions = self._sessions_for(scope)
        if self._persistence is not None:
            sessions = self._persistence.session_store_for(scope)
            # FR-11: exactly one manifest row, written at start -- in the same
            # transaction as the run row, so no failure between the two can
            # leave a run that nothing can explain. The primary key guarantees
            # "at most one"; passing it here guarantees "at least one".
            await control.store(
                partial(self._persistence.runs.start_run, scope),
                agent_spec_id=spec.id,
                max_turns=config.max_turns,
                model_id=recorded_model,
                principal_context=(
                    config.principal_context.to_json() if config.principal_context else None
                ),
                parent_run_id=config.parent_run_id,
                manifest=build_manifest(
                    sdk_version=__version__,
                    agent_spec_id=spec.id,
                    instructions=spec.instructions,
                    tool_profile=spec.tool_profile,
                    tool_spec_hashes=[s.schema_hash() for s in self._registry.specs()],
                    tools_sent=[{"name": s.name, "schema_hash": s.schema_hash()} for s in registry.specs()],
                    gate_policy=None if config.gate_policy is None else config.gate_policy.to_json(),
                    approval_timeout=float(config.approval_timeout),
                    mcp_servers=[] if self._mcp is None else self._mcp.records(),
                    workspace=None if spec.workspace is None else {
                        "provider": None if self._workspaces is None else self._workspaces.provider.name,
                        **spec.workspace.record()},
                    model_id=recorded_model or "unspecified",
                    **self._model_versions(recorded_model, client_key),
                    policy_version=type(spec.checker()).__name__,
                    max_output_tokens=max_output_tokens,
                    reasoning_effort=reasoning_effort.value if reasoning_effort is not None else None,
                    pricing=self._pricing_record(recorded_model),
                    scheduler_limits=limits.to_json(),
                    **budget_record,
                ),
            )

        # EVERY store call leaves the loop, not most of them (FR-20). M7 round 1
        # found this emit, ToolCalled's and the terminal one still made on the
        # loop -- four per run, scaling with fan-out -- and each takes the event
        # stream's advisory lock, so a lock held by another process became a
        # whole-loop stall: 479 ms, freezing unrelated runs with it, where the
        # messages path that HAD been offloaded stalled 14 ms under the same
        # probe. Moving three of four store paths off the loop is the defect
        # this comment exists to prevent recurring.
        await control.store(
            events.emit,
            EventType.RUN_STARTED,
            {
                "agent_spec_id": spec.id,
                "model": recorded_model,
                "provider": client_key,
                "max_turns": config.max_turns,
                # FR-57: so a run's own events name its parent (FR-21), None at the top.
                "parent_run_id": config.parent_run_id,
                # Recorded, never read in Phase 0 (ADR-27).
                "principal_context": (
                    config.principal_context.to_json() if config.principal_context else None
                ),
            },
        )

        # FR-138 (M38): a run whose agent has a workspace and was given none fails before its first
        # model call, with no workspace tool offered.
        if spec.workspace is not None and run_id not in self._workspace_runs:
            raise WorkspaceUnavailable(self._workspace_refusal(spec.workspace))

        # FR-44: the calls of a parallel batch can finish together. Emitting one
        # ToolCalled of this run at a time keeps their events in the order they
        # finish on both stores, beside the sinks' own numbering locks (FR-52).
        # Each carries its call's id in the envelope, which had stayed NULL since
        # Phase 0.
        tool_events = asyncio.Lock()

        async def emit_tool_called(event_type: str, payload: dict[str, Any]) -> None:
            call_id = payload.get("tool_call_id")
            async with tool_events:
                await control.store(
                    events.emit,
                    EventType.TOOL_CALLED,
                    payload,
                    tool_call_id=call_id if isinstance(call_id, str) else None,
                )

        artifacts = AttributedArtifacts(
            partial(self._artifacts_for, scope.tenant_id, scope.project_id), run_id, config.node_id
        )

        async def taken_in() -> tuple[ContentProvenance, ...]:
            # FR-93: what the run has taken in when a claim is recorded -- every tool
            # result already in its history and every input it was briefed with.
            history = await control.store(sessions.history, run_id)
            # Briefed inputs first: cite-by-URL takes the last match, and the run's own
            # tool results are newer than what it was briefed with (M23 review caveat 3).
            return tuple(p for _, p in config.briefed_inputs) + tuple(
                r.provenance for m in history for r in m.tool_results)

        ledger_run_id = config.ledger_run_id or run_id
        executor = ToolExecutor(
            registry=registry,
            # P4-D30 (M33): with MCP servers, `<alias>__*` names the tools a configured server
            # listed, read from this run's own view (round 6).
            permission_checker=(spec.checker() if self._mcp is None or spec.permission_policy is not None
                                else self._mcp.checker(spec.checker(), registry)),
            hook=self._hook,
            # A coroutine, which the executor awaits: see ToolExecutor._safe_emit.
            emit=emit_tool_called,
            # FR-76: what a tool declaring a RunScope parameter receives.
            scope=ToolScope(
                run_id=run_id, tenant_id=scope.tenant_id, project_id=scope.project_id,
                node_id=config.node_id,
                artifacts=artifacts,
                ledger_run_id=ledger_run_id,
                evidence=ResourceCache(
                    self._evidence_store, self._artifacts_for,
                    tenant_id=scope.tenant_id, project_id=scope.project_id, run_id=run_id,
                    ledger_run_id=ledger_run_id, node_id=config.node_id,
                    call=control.store, emit=partial(control.store, events.emit), lock=self._claim_lock,
                ),
                agent_id=spec.id,
                ledger=RunLedger(
                    self._ledger_store, self._evidence_store,
                    tenant_id=scope.tenant_id, project_id=scope.project_id, ledger_run_id=ledger_run_id,
                    run_id=run_id, node_id=config.node_id, agent_id=spec.id,
                    call=control.store, taken_in=taken_in, emit=partial(control.store, events.emit),
                    lock=self._claim_lock,
                ),
                _events=events, _control=control, _runner=self, _lease=config.budget_lease,
                _approval_timeout=float(config.approval_timeout),
            ),
        )

        def cost_of(request: ModelRequest, usage: Usage) -> Decimal | None:
            # Priced by the model the request was actually sent with, which a
            # before_model hook may have changed (FR-30): decided by the same function
            # that names it in ModelCalled (FR-57; M14 review round 1, C2).
            model = sent_model(request, recorded_model)
            try:
                pricing, table_date = self._effective_pricing(model)
                if table_date is not None:
                    # The shipped table priced this call, whichever model a hook sent
                    # it with, so its date is what the manifest must record (H5).
                    meter.price_table_date = table_date
                return call_cost(usage, pricing)
            except Exception:  # noqa: BLE001 - accounting never fails a run (NFR-11)
                return None

        loop = AgentLoop(
            model_client=self._clients[client_key],
            session_store=sessions,
            tool_executor=executor,
            tool_registry=registry,
            event_sink=events,
            assembler=self._assembler,
            hook=self._hook,
            cost_of=cost_of,
            budget=config.budget_lease,
            output_schema=config.output_schema,
            briefed_inputs=config.briefed_inputs,
            meter=meter,
            tool_slot=RunSlots(limits).slot,
            model_slot=partial(self._provider_slots.slot, client_key),
            control=control,
            provider=client_key,
            provider_name=provider_name,
            recorded_model=recorded_model,
            # FR-78: a compaction records what it replaced as this run's artifact.
            compactor=None if window is None else ContextCompactor(policy, window),
            artifacts=artifacts,
            agent_id=spec.id,
            interruptions=self._interruption_store,
            approval_waits=self._approvals,
            approval_timeout=float(config.approval_timeout),
            emit_top=self._top_emitter(run_id, ledger_run_id, control),
            tools_refresh=getattr(registry, "refresh", None),
        )

        # Only what the caller set: a run that sets none of M9's options sends
        # exactly the request it sent before M9 (NFR-12).
        model_settings: dict[str, Any] = {"model": model_id} if model_id else {}
        if max_output_tokens is not None:
            model_settings["max_tokens"] = max_output_tokens
        if reasoning_effort is not None:
            model_settings["reasoning_effort"] = reasoning_effort.value

        outcome = await loop.run(
            run_id,
            task,
            max_turns=config.max_turns,
            instructions=spec.instructions,
            model_settings=model_settings,
            principal_context=config.principal_context,
        )

        if outcome.exhausted_turns:
            status, error = RunStatus.MAX_TURNS_EXCEEDED, "max_turns_exceeded"
        elif outcome.error is not None:
            status, error = RunStatus.FAILED, outcome.error
        else:
            status, error = RunStatus.COMPLETED, None

        # FR-50: a cancellation that arrived during the loop's last store writes
        # takes effect here, before the terminal event. Once that event is being
        # written, a cancellation has no effect.
        if control.requested:
            raise asyncio.CancelledError()
        await self._end_workspace(run_id, events, control)
        control.terminal = True
        await control.store(
            events.emit,
            EventType.RUN_COMPLETED if status is RunStatus.COMPLETED else EventType.RUN_FAILED,
            {"status": status.value, "turns": outcome.turns, "reason": error, **_run_timing(control)},
        )
        control.terminal_written = True
        if self._persistence is not None:
            # NOT swallowed, unlike the failure and cancellation paths: a run recorded
            # as still running is a lie the caller should hear about (M4). What round 4
            # found (J1) is what happens next -- see the handler in run(), which must
            # not write a second terminal event over this one.
            await control.store(
                self._finish, scope, status, meter.usage, meter.cost_usd,
                self._spend_record(config.budget_lease, meter.usage, meter.cost_usd, meter.budget_tokens),
                meter.price_table_date,
            )
        return RunResult(
            status=status,
            output=outcome.output,
            events=events.events(),
            usage=meter.usage,
            run_id=run_id,
            error=error,
            cost_usd=meter.cost_usd,
        )

    def _history_for(self, scope: RunScope) -> list[Any]:
        """The messages a run left behind, from wherever that run's session lives.

        Not application API: `SubagentPool` uses it to find out what a child took in
        through its own tools, which decides the provenance its answer carries (FR-70).
        """
        return list(self._sessions_for(scope).history(scope.run_id))

    def _sessions_for(self, scope: RunScope) -> Any:
        """The session store bound to one run. FR-87 (M21): the in-memory store is
        bound as the Postgres one is, so it knows the run's tenant and project; a
        caller's own store, which the Runner cannot bind, is used as it was given."""
        if self._persistence is not None:
            return self._persistence.session_store_for(scope)
        if isinstance(self._sessions, InMemorySessionStore):
            return self._sessions.bind(scope)
        return self._sessions

    def _limits_for(self, config: RunConfig) -> SchedulerLimits:
        """The limits a run executes under (FR-43): its own per-run and per-tool
        limits as a whole when its RunConfig sets them, and always the Runner's
        provider limits."""
        chosen = config.scheduler_limits
        if chosen is None:
            return self._limits
        return SchedulerLimits(
            max_concurrent_tools=chosen.max_concurrent_tools,
            tool_concurrency_limits=chosen.tool_concurrency_limits,
            provider_concurrency_limits=self._limits.provider_concurrency_limits,
            # FR-72's limits are the run's own too. Rebuilding from three fields left
            # the other three at their defaults, so a child recorded limits that never
            # governed it (round 1, L2).
            max_concurrent_subagents=chosen.max_concurrent_subagents,
            max_tasks_per_run=chosen.max_tasks_per_run,
            queue_policy=chosen.queue_policy,
        )

    def _effective_pricing(self, model_id: str | None) -> tuple[Any, str | None]:
        """The prices a model is costed with, and the shipped table's date when that
        table is what supplied them (FR-69).

        The caller's registry wins; the shipped table is the default behind it; a model
        in neither stays unpriced, which is not the same as free.
        """
        entry = self._models.resolve(model_id) if model_id else None
        pricing = entry.capabilities.pricing if entry is not None else None
        if pricing is not None:
            return pricing, None
        shipped = shipped_pricing(model_id)
        return (shipped, PRICE_TABLE_DATE if shipped is not None else None)

    def _cost(self, model_id: str | None, usage: Usage) -> Decimal | None:
        """What `usage` cost on `model_id`, or None when that cannot be known.

        Guarded as a whole, the registry lookup included: the registry is the
        caller's, and accounting never fails a run (NFR-11).
        """
        try:
            pricing, _ = self._effective_pricing(model_id)
            return call_cost(usage, pricing)
        except Exception:  # noqa: BLE001
            return None

    def _pricing_record(self, model_id: str | None) -> dict[str, str | None] | None:
        """The prices a run is costed with, for its manifest (FR-31)."""
        pricing, _ = self._effective_pricing(model_id)
        return pricing.to_json() if pricing is not None else None

    def _budget_record(self, lease: Any, model_id: str | None) -> dict[str, Any]:
        """FR-67 to FR-69: the policy and reservation this run runs under, for its
        manifest, and the refusal when a USD ceiling meets a model nothing prices.

        Raised here, at the call site, rather than discovered as an unpriced run
        afterwards: a USD budget that cannot be measured is a configuration error.
        """
        if lease is None:
            return {}
        policy = lease.governor.policy
        if policy.run_ceiling_usd is not None and self._effective_pricing(model_id)[0] is None:
            raise UnpricedModel(
                f"a USD budget needs a price for {model_id!r}: the model registry has none and the"
                f" shipped price table of {PRICE_TABLE_DATE} does not list it"
            )
        reservation = lease.reservation
        return {
            "budget_policy": {
                "run_ceiling_usd": _decimal_text(policy.run_ceiling_usd),
                "run_ceiling_tokens": policy.run_ceiling_tokens,
                "orchestrator_reserve_fraction": _decimal_text(policy.orchestrator_reserve_fraction),
                "unallocated_reserve_fraction": _decimal_text(policy.unallocated_reserve_fraction),
                "reservation_cap_fraction": _decimal_text(policy.reservation_cap_fraction),
                "max_replans": policy.max_replans,
            },
            "budget_reservations": {
                lease.node_id: {"usd": _decimal_text(reservation.usd), "tokens": reservation.tokens}
            },
        }

    def _default_model_id(self, client_key: str) -> str | None:
        """The client's own default model, used when no model was named (FR-32).

        Optional on the client: ModelClient stays send()-only. A client without
        the attribute -- or with one that raises, or holds something no column
        can store -- records no model id, exactly as before M9.
        """
        try:
            candidate = getattr(self._clients[client_key], "default_model_id", None)
        except Exception:  # noqa: BLE001 - a client property is caller code
            return None
        if not isinstance(candidate, str) or not candidate:
            return None
        return candidate if column_rejection_reason(candidate, "TEXT") is None else None

    def _provider_name(self, client_key: str) -> str | None:
        """The provider name the client declares, read once per run (FR-57).

        Optional, as default_model_id is (FR-32): ModelClient stays send()-only. A client
        without the attribute, or with one that raises or holds something no column can
        store, records None.
        """
        try:
            candidate = getattr(self._clients[client_key], "provider_name", None)
        except Exception:  # noqa: BLE001 - a client property is caller code
            return None
        if type(candidate) is not str or not candidate:
            return None
        return candidate if column_rejection_reason(candidate, "TEXT") is None else None

    def _context_window(self, config: RunConfig, model_id: str | None) -> int | None:
        """FR-78: the window a run's policy compacts against, None when it never does.

        The policy's own, else the ModelRegistry's for the run's model; a compacting
        policy on a model with neither raises, naming the model (DECISION-468e2bfa).
        """
        if config.context_policy is None:
            return None
        entry = self._models.resolve(model_id) if model_id else None
        registered = entry.capabilities.max_context_tokens if entry is not None else None
        return config.context_policy.window_for(model_id, registered)

    def _model_versions(self, model_id: str | None, client_key: str) -> dict[str, str]:
        """Version fields for the manifest (FR-11, AC-6).

        A model absent from the registry is reported as "unregistered" rather
        than left null: the manifest's job is to say exactly what produced a
        run, and "we did not know" is a more useful answer than an empty
        column that could equally mean the writer forgot.
        """
        entry = self._models.resolve(model_id) if model_id else None
        return {
            "model_version": entry.model_version if entry else "unregistered",
            "model_adapter_version": (
                entry.adapter_version if entry else type(self._clients[client_key]).__name__
            ),
        }

    def _resolve_model(self, spec: AgentSpec, config: RunConfig) -> tuple[str, str | None]:
        """`"<client_key>:<model_id>"`, or a bare model id when unambiguous."""
        reference = config.model_override or spec.preferred_model
        if not reference:
            if len(self._clients) != 1:
                raise ValueError(
                    "no preferred_model or model_override given and multiple model "
                    f"clients are registered: {sorted(self._clients)}"
                )
            return next(iter(self._clients)), None

        key, separator, model_id = reference.partition(":")
        if not separator:
            if len(self._clients) != 1:
                raise ValueError(
                    f"model reference {reference!r} has no '<client>:' prefix and "
                    f"multiple clients are registered: {sorted(self._clients)}"
                )
            return next(iter(self._clients)), reference
        if key not in self._clients:
            raise ValueError(
                f"unknown model client {key!r}; registered: {sorted(self._clients)}"
            )
        return key, model_id
