"""Conversation history (FR-9, LLD 3.9).

Phase 0 is insert-only: append and read back in order, no update, no delete.
The Postgres implementation lands in M5 behind this same protocol; the
in-memory one exists so the loop is testable without a database and so M5 is a
new implementation rather than a new interface.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

from .primitives import Message


@runtime_checkable
class SessionStore(Protocol):
    def append(self, run_id: str, message: Message) -> None: ...

    def history(self, run_id: str) -> list[Message]: ...


_Key = tuple[Any, Any, str]

# The run a bound view is acting for, while it calls the store's own public methods.
# Through those methods, so a wrapper or a test double on InMemorySessionStore.append
# still sees every write the Runner makes; a context variable, so it follows the call
# onto the worker thread that runs it (asyncio.to_thread copies the context).
_BINDING: ContextVar[_Key | None] = ContextVar("agentsdk_session_binding", default=None)


def _refuse_foreign(sink: Any, named: tuple[Any, Any, str]) -> bool:
    """FR-87 (M21): whether a combined write may go through `sink` in one piece.

    A sink that names no scope -- none at all, or None, as a PublishingSink over a
    scope-less sink does -- gets the two writes it got before M20a (F1). A sink that
    names a scope must name this run: its tenant and project too, where they are
    known, or it is refused before anything is written (F2)."""
    scope = getattr(sink, "scope", None)
    if scope is None:
        return False
    tenant, project, run_id = named
    if scope[2] != run_id or (tenant is not None and (scope[0], scope[1]) != (tenant, project)):
        raise ValueError("the event sink does not write to this run; message and event must be one run's")
    return True


class InMemorySessionStore:
    """FR-9 in memory. A run's history is keyed by its tenant, project and run id
    (FR-87): the Runner binds the store to each run, as the Postgres store is bound,
    so two tenants' runs never share a history. Called unbound -- by a test or an
    application driving the loop itself -- it is keyed by run id alone, as before, and
    the first tenant and project to bind that run id adopt what was written to it
    unbound (FR-97, M21a), as the store merged them before M21."""

    def __init__(self) -> None:
        self._runs: dict[_Key, list[Message]] = {}
        # M21 round 1: run id -> the keys it is stored under, so an unbound call finds
        # its run without scanning every run, and one lock over both. Store calls run
        # on worker threads; before M21 every operation here was a single dict call,
        # and a scan of the dict while other threads added runs raised.
        self._by_run: dict[str, list[_Key]] = {}
        self._lock = threading.Lock()

    def bind(self, scope: Any) -> _BoundInMemorySessions:
        """A per-run view, knowing the run's tenant and project."""
        return _BoundInMemorySessions(self, (scope.tenant_id, scope.project_id, scope.run_id))

    def _messages(self, key: _Key) -> list[Message]:
        with self._lock:
            return self._runs[self._held(key, create=True)]

    def _held(self, key: _Key, *, create: bool) -> _Key:
        """The key a run's history is stored under, the lock held. An unbound key means
        the one run of its id; a bound key not yet stored adopts the id's unbound
        history (FR-97), moving the list itself, so a write already holding it lands in
        the adopted history. Resolved here, under the lock, so no write in between can
        file one run id under two keys."""
        run_id = key[2]
        found = self._by_run.get(run_id, [])
        unbound = (None, None, run_id)
        if key == unbound:
            if len(found) > 1:
                raise ValueError(f"run {run_id!r} exists in more than one tenant or project; bind the store to read it")
            if found:
                return found[0]
        elif key not in self._runs and unbound in self._runs:
            self._runs[key] = self._runs.pop(unbound)
            found[found.index(unbound)] = key
            return key
        if create and key not in self._runs:
            self._runs[key] = []
            self._by_run.setdefault(run_id, []).append(key)
        return key

    def _key(self, run_id: str) -> _Key:
        """The run a call means: the bound one, or the one run of that id, whichever
        tenant it belongs to, or a run nothing bound. Two tenants' runs sharing an id
        cannot be told apart without a scope, so that is refused rather than guessed."""
        bound = _BINDING.get()
        if bound is not None:
            if bound[2] != run_id:
                raise ValueError(f"this session store is bound to run {bound[2]!r}, not {run_id!r}")
            return bound
        with self._lock:
            return self._held((None, None, run_id), create=False)

    def append(self, run_id: str, message: Message) -> None:
        key = self._key(run_id)
        with self._lock:
            self._runs[self._held(key, create=True)].append(message)

    def append_with_event(
        self, run_id: str, message: Message, sink: Any, event_type: Any, payload: dict[str, Any]
    ) -> Any:
        """FR-86 (M20a): a message and the event that explains it, both or neither --
        a compaction's summary and its ContextCompacted. An event that cannot be
        recorded takes the message back out, whatever the sink (FR-87)."""
        key = self._key(run_id)
        _refuse_foreign(sink, key)
        # FR-97 (M21a): the run's history taken once, before the write, and the write
        # bound to that key, so the append and the take-back act on one list however a
        # first bound write of this run interleaves. Still through append, for its wrappers.
        messages = self._messages(key)
        token = _BINDING.set(key)
        try:
            self.append(run_id, message)
        finally:
            _BINDING.reset(token)
        try:
            return sink.emit(event_type, payload)
        except BaseException:
            with self._lock:
                for index in range(len(messages) - 1, -1, -1):
                    if messages[index] is message:
                        del messages[index]
                        break
            raise

    def history(self, run_id: str) -> list[Message]:
        # A copy: callers must not be able to mutate stored history by holding
        # the list, which would make the append-only invariant a lie.
        key = self._key(run_id)
        with self._lock:
            return list(self._runs.get(self._held(key, create=False), ()))


class _BoundInMemorySessions:
    """One run's view of an InMemorySessionStore (FR-87): every call goes through the
    store's own public method, acting for this run."""

    def __init__(self, store: InMemorySessionStore, key: _Key) -> None:
        self._store, self._key = store, key

    def _as_run(self, method: Any, *args: Any) -> Any:
        token = _BINDING.set(self._key)
        try:
            return method(*args)
        finally:
            _BINDING.reset(token)

    def append(self, run_id: str, message: Message) -> None:
        self._as_run(self._store.append, run_id, message)

    def append_with_event(
        self, run_id: str, message: Message, sink: Any, event_type: Any, payload: dict[str, Any]
    ) -> Any:
        return self._as_run(self._store.append_with_event, run_id, message, sink, event_type, payload)

    def history(self, run_id: str) -> list[Message]:
        return self._as_run(self._store.history, run_id)
