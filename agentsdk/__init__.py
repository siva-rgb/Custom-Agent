"""Custom Agent SDK - Phase 0 (single-agent skeleton).

Application code should import from this package root only. Internal
collaborators (AgentLoop, ToolExecutor, ...) are deliberately not re-exported:
the public surface is AgentSpec / RunConfig / Runner / RunResult (NFR-5).
"""

from .api import AgentSpec, RunConfig, Runner, RunResult, RunStatus
from .artifacts import ArtifactRef, ArtifactStore, InMemoryArtifactStore
from .budget import BudgetAllocation, BudgetAmount, BudgetGovernor, BudgetLease, BudgetPolicy
from .handle import RunHandle, RunState
from .model import ReasoningEffort
from .persistence import Persistence
from .plan import (
    AcceptanceCriterion,
    BudgetReservation,
    InMemoryRunStateStore,
    NodeRetryPolicy,
    PlanNode,
    PlanVersion,
    RunStateStore,
    plan_from_document,
)
from .scheduler import SchedulerLimits
from .subagents import Briefing, SubagentPool, SubagentResult
from .orchestrator import Orchestrator
from .scope import ToolScope
from .context_policy import ContextPolicy
from .compaction import ContextCompactor
from .evidence import (
    CacheScope,
    EvidenceSourceVersion,
    EvidenceStore,
    InMemoryEvidenceStore,
    ResourceCache,
    ResourceCacheKey,
)
from .errors import (
    AgentSDKError,
    ArtifactIntegrityError,
    InvalidPlan,
    PlanIntegrityError,
    PlanNotFound,
    UnpricedModel,
    ArtifactNotFound,
    BudgetExceeded,
    Cancelled,
    DependencyFailed,
    InvalidStructuredOutput,
    MaxDepthExceeded,
    MaxTurnsExceeded,
    ModelError,
    ModelProviderUnavailable,
    ModelRateLimited,
    ModelTimeout,
    ReplanLimitExceeded,
    ToolApprovalRequired,
    ToolCancelled,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolPermissionDenied,
    ToolTimeout,
    ToolValidationError,
    WorkflowError,
)
from .primitives import (
    ContentProvenance,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
)

from .version import __version__

__all__ = [
    # The public API surface (NFR-5). Everything below it is exported for
    # authoring tools and specs, not for driving a run.
    "AgentSpec",
    "RunConfig",
    "Runner",
    "RunResult",
    # Opting into persistence is part of driving a run, so it belongs on the
    # public surface. Requiring `import agentsdk.persistence` for it made
    # NFR-5's "application code calls Runner.run() and nothing else" false in
    # the one place every real caller has to go.
    "Persistence",
    "RunStatus",
    # What Runner.start returns, and its snapshot (FR-48, FR-49): driving a run
    # that way needs them, so they belong beside Runner.
    "RunHandle",
    "RunState",
    # A field of AgentSpec and RunConfig (FR-28), so it belongs beside them.
    "ReasoningEffort",
    # An argument of Runner and a field of RunConfig (FR-43), for the same reason.
    "SchedulerLimits",
    # Artifacts (FR-53, FR-54): the in-memory store is built directly; the
    # Postgres one comes from Persistence.artifact_store.
    "ArtifactRef",
    "ArtifactStore",
    "InMemoryArtifactStore",
    # Plans (FR-64 to FR-66): the in-memory run-state store is built directly; the
    # Postgres one comes from Persistence.run_state_store.
    "PlanNode",
    "PlanVersion",
    # Budgets (FR-67, FR-68): the governor holds a run's ceiling and hands a
    # lease to each node; M19's orchestrator is what sets RunConfig.budget_lease.
    "BudgetPolicy",
    "BudgetAmount",
    "BudgetAllocation",
    "BudgetGovernor",
    "BudgetLease",
    # Subagents (FR-70 to FR-72): the pool runs a child for a parent run; M19's
    # orchestrator is its intended caller.
    "Briefing",
    "SubagentPool",
    "SubagentResult",
    # The orchestrator (FR-73 to FR-75) and the scope a tool reaches its run through (FR-76).
    "Orchestrator",
    "ToolScope",
    # What an agent sees and when its history compacts (FR-77, FR-78): a field of RunConfig.
    "ContextPolicy",
    "ContextCompactor",
    "CacheScope",
    "EvidenceSourceVersion",
    "EvidenceStore",
    "InMemoryEvidenceStore",
    "ResourceCache",
    "ResourceCacheKey",
    "AcceptanceCriterion",
    "BudgetReservation",
    "NodeRetryPolicy",
    "plan_from_document",
    "RunStateStore",
    "InMemoryRunStateStore",
    "ContentProvenance",
    "InstructionAuthority",
    "Message",
    "Origin",
    "Role",
    "TaintFlag",
    "ToolCall",
    "ToolResult",
    "TrustZone",
    "AgentSDKError",
    "ModelError",
    "ModelTimeout",
    "ModelRateLimited",
    "ModelProviderUnavailable",
    "InvalidStructuredOutput",
    "ToolError",
    "ToolNotFound",
    "ToolValidationError",
    "ToolPermissionDenied",
    "ToolApprovalRequired",
    "ToolTimeout",
    "ToolExecutionError",
    "ToolCancelled",
    "ArtifactNotFound",
    "ArtifactIntegrityError",
    "InvalidPlan",
    "PlanIntegrityError",
    "UnpricedModel",
    "PlanNotFound",
    "WorkflowError",
    "BudgetExceeded",
    "MaxTurnsExceeded",
    "MaxDepthExceeded",
    "DependencyFailed",
    "ReplanLimitExceeded",
    "Cancelled",
    "__version__",
]
