"""FR-76 (M19): how a tool reaches the run that called it.

A tool that declares a parameter annotated `RunScope` (or `ToolScope`) receives one; a
tool that declares none is called exactly as before, so no existing tool changes and no
`schema_hash` moves. The scope is an explicit argument rather than a context variable
because an explicit argument is what a test can pin (P2-D26).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .postgres import RunScope
from .primitives import unstorable_reason

__all__ = ["AttributedArtifacts", "ToolScope"]


@dataclass(frozen=True)
class ToolScope(RunScope):
    """A RunScope for a tool: the run's ids, the plan node when one applies, and the
    artifact store bound to that run.

    A subclass rather than more fields on RunScope, because RunScope checks every one of
    its fields for storability (DECISION-53461588) and a store is not a value. The
    underscored fields are the orchestrator's (M19): its plan tool needs the run's event
    stream, control, Runner and budget lease, and an ordinary tool has no use for them.
    """

    node_id: str | None = None
    artifacts: Any = field(default=None, compare=False, repr=False)
    _events: Any = field(default=None, compare=False, repr=False)
    _control: Any = field(default=None, compare=False, repr=False)
    _runner: Any = field(default=None, compare=False, repr=False)
    _lease: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        # The ids are refused exactly as RunScope refuses them.
        RunScope(run_id=self.run_id, tenant_id=self.tenant_id, project_id=self.project_id)
        if self.node_id is not None and (
            type(self.node_id) is not str or not self.node_id or unstorable_reason(self.node_id) is not None
        ):
            raise ValueError(f"node_id must be None or a non-empty storable str, got {self.node_id!r}")


class AttributedArtifacts:
    """A run's artifact store that writes every artifact as this run's and node's.

    FR-76: "the artifact it writes through that scope is attributed to the run and node
    that wrote it". The attribution is set here, over anything the tool passed, so a tool
    cannot write an artifact in another run's name. Reads go to the store unchanged.

    The store is built on first use, so a run whose tools never touch it is exactly the
    run it was before M19 (NFR-12's rule).
    """

    def __init__(self, store: Callable[[], Any], run_id: str, node_id: str | None) -> None:
        self._make, self._store, self._run_id, self._node_id = store, None, run_id, node_id

    def _bound(self) -> Any:
        if self._store is None:
            self._store = self._make()
        return self._store

    async def put(self, content: Any, **options: Any) -> Any:
        options["source_run"] = self._run_id
        options["source_task"] = self._node_id
        return await self._bound().put(content, **options)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bound(), name)
