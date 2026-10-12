"""The confidence gate's record and rules (FR-100 to FR-102, M26).

An `EvidenceVerification` is an append-only judgement of a claim or a plan node: the
gate's (did its rule signals fire), a critic's (M27), or an externally validated outcome
recorded by the application. It is read beside what it judges and never folded into it,
so no verification makes a claim or an answer cleaner (P3-D15, NFR-28).

`GatePolicy` decides which nodes are gated and what fires. The signals are pure functions
of the ledger as read -- no model call -- so a node whose signals are clean is cleared
without one (P3-D17, P3-D18).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .artifacts import canonical_id
from .primitives import ContentProvenance, unstorable_reason

__all__ = [
    "EXTERNAL_SOURCES",
    "SIGNALS",
    "EvidenceVerification",
    "GatePolicy",
    "NodeSubject",
    "signals_of",
]

# FR-102, in the order they are reported.
SIGNALS = ("unsupported", "contradicted", "thin_support", "low_confidence", "no_claims")
# FR-100: each kind's outcomes.
OUTCOMES = {
    "gate": ("clear", "critic_required"),
    "critic": ("approved", "corrected", "escalate"),
    "external": ("correct", "incorrect"),
}
EXTERNAL_SOURCES = ("test", "fixture", "downstream", "human")
CRITIC_NODE_SUFFIX = "#critic"  # FR-104: the node id a critic run carries
_CLAIM_URN = "urn:agentsdk:claim:"
_VERIFICATION_URN = "urn:agentsdk:verification:"


def _id(name: str, value: Any) -> str:
    key = canonical_id(value) if isinstance(value, str) else None
    if key is None:
        raise ValueError(f"{name} must be a canonical UUID as text, got {value!r}")
    return key


def _text(name: str, value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be non-empty text, got {value!r}")
    reason = unstorable_reason(value)
    if reason is not None:
        raise ValueError(f"{name} cannot be stored: {reason}")
    return value


def _json_object(name: str, value: Any) -> dict[str, Any]:
    """A JSON object a jsonb column holds exactly: no NaN or infinity, no text it cannot."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object, got {type(value).__name__}")
    try:
        text = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a JSON object: {exc}") from None
    reason = unstorable_reason(text)
    if reason is not None:
        raise ValueError(f"{name} cannot be stored: {reason}")
    return json.loads(text)


@dataclass(frozen=True)
class NodeSubject:
    """A plan node, as a verification names it: the orchestrator run, its plan and
    version, and the node."""

    run_id: str
    plan_id: str
    version: int
    node_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _id("run_id", self.run_id))
        object.__setattr__(self, "plan_id", _id("plan_id", self.plan_id))
        if type(self.version) is not int or not 1 <= self.version <= 2**31 - 1:
            raise ValueError(f"version must be an int of 1 or more, got {self.version!r}")
        _text("node_id", self.node_id)

    def to_json(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "plan_id": self.plan_id, "version": self.version, "node_id": self.node_id}


@dataclass(frozen=True)
class EvidenceVerification:
    """FR-100: one judgement of a claim (by its uri) or a node, written once."""

    verification_id: str
    ledger_run_id: str
    subject: str | NodeSubject
    kind: str
    outcome: str
    detail: Mapping[str, Any]
    provenance: ContentProvenance
    tenant_id: str
    project_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "verification_id", _id("verification_id", self.verification_id))
        object.__setattr__(self, "ledger_run_id", _id("ledger_run_id", self.ledger_run_id))
        if type(self.kind) is not str or self.kind not in OUTCOMES:
            raise ValueError(f"kind must be one of {', '.join(OUTCOMES)}, got {self.kind!r}")
        if isinstance(self.subject, str):
            if not self.subject.startswith(_CLAIM_URN):
                raise ValueError(f"subject must be a claim's uri or a NodeSubject, got {self.subject!r}")
            object.__setattr__(self, "subject", _CLAIM_URN + _id("subject", self.subject[len(_CLAIM_URN):]))
        elif not isinstance(self.subject, NodeSubject):
            raise ValueError(f"subject must be a claim's uri or a NodeSubject, got {self.subject!r}")
        if self.kind == "gate" and not isinstance(self.subject, NodeSubject):
            raise ValueError("subject of a gate verification must be a node: the gate judges a node")
        if type(self.outcome) is not str or self.outcome not in OUTCOMES[self.kind]:
            raise ValueError(f"outcome of a {self.kind} verification must be one of "
                             f"{', '.join(OUTCOMES[self.kind])}, got {self.outcome!r}")
        detail = _json_object("detail", self.detail)
        if self.kind == "external":
            if detail.get("source") not in EXTERNAL_SOURCES:
                raise ValueError(f"detail of an external verification must name its source, one of "
                                 f"{', '.join(EXTERNAL_SOURCES)}, got {detail.get('source')!r}")
            try:
                _text("detail", detail.get("detail"))
            except ValueError:
                raise ValueError("detail of an external verification must say who or what validated it, "
                                 "and how, as non-empty text") from None
        object.__setattr__(self, "detail", detail)
        if not isinstance(self.provenance, ContentProvenance):
            raise ValueError(f"provenance must be a ContentProvenance, got {type(self.provenance).__name__}")
        _text("tenant_id", self.tenant_id)
        _text("project_id", self.project_id)
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be a timezone-aware datetime")

    @property
    def uri(self) -> str:
        return f"{_VERIFICATION_URN}{self.verification_id}"

    @property
    def claim_id(self) -> str | None:
        """The claim judged, or None for a node."""
        return self.subject[len(_CLAIM_URN):] if isinstance(self.subject, str) else None


@dataclass(frozen=True)
class GatePolicy:
    """FR-101: which nodes are gated, which signals may fire and at what, and the critic.

    A critic has no tools, so it judges only what it is briefed with (DECISION-2cec3b61);
    gating every node needs one, since a node it gates could otherwise be neither cleared
    nor criticised."""

    gate_all_nodes: bool = False
    signals: tuple[str, ...] = SIGNALS
    min_sources: int = 1
    confidence_threshold: float = 0.7
    max_briefing_chars: int = 200_000
    critic: Any = field(default=None)

    def __post_init__(self) -> None:
        from .api import AgentSpec

        if type(self.gate_all_nodes) is not bool:
            raise ValueError(f"gate_all_nodes must be a bool, got {self.gate_all_nodes!r}")
        signals = self.signals
        if isinstance(signals, (str, bytes)) or not isinstance(signals, (list, tuple, frozenset, set)):
            raise ValueError(f"signals must be a collection of signal names, got {signals!r}")
        names = list(signals)
        unknown = [s for s in names if s not in SIGNALS]
        if unknown or len(set(names)) != len(names):
            raise ValueError(f"signals must name each of {', '.join(SIGNALS)} at most once, got {names!r}")
        object.__setattr__(self, "signals", tuple(s for s in SIGNALS if s in names))
        if type(self.min_sources) is not int or not 1 <= self.min_sources <= 2**31 - 1:
            raise ValueError(f"min_sources must be an int of 1 or more, got {self.min_sources!r}")
        threshold = self.confidence_threshold
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) \
                or not 0 <= threshold <= 1:
            raise ValueError(f"confidence_threshold must be a number from 0 to 1, got {threshold!r}")
        if type(self.max_briefing_chars) is not int or not 1 <= self.max_briefing_chars <= 2**31 - 1:
            raise ValueError(f"max_briefing_chars must be an int of 1 or more, got {self.max_briefing_chars!r}")
        if self.critic is not None:
            if not isinstance(self.critic, AgentSpec):
                raise ValueError(f"critic must be an AgentSpec or None, got {type(self.critic).__name__}")
            if self.critic.tool_profile:
                raise ValueError(f"critic must have no tools, so it judges only its briefing; "
                                 f"{self.critic.id!r} has {list(self.critic.tool_profile)}")
        elif self.gate_all_nodes:
            raise ValueError("gate_all_nodes needs a critic: a node it gates could be neither cleared nor criticised")

    def gates(self, criteria: Any) -> bool:
        """Whether a node with these acceptance criteria is gated (P3-D17, P3-D20)."""
        if self.critic is None:
            return False
        return self.gate_all_nodes or any(c.kind == "critic" for c in criteria)

    def to_json(self) -> dict[str, Any]:
        """What the manifest records (FR-101)."""
        from .manifest import agent_spec_hash

        critic = None if self.critic is None else {
            "id": self.critic.id,
            "spec_hash": agent_spec_hash(self.critic.id, self.critic.instructions, self.critic.tool_profile),
        }
        return {"gate_all_nodes": self.gate_all_nodes, "signals": list(self.signals),
                "min_sources": self.min_sources, "confidence_threshold": self.confidence_threshold,
                "max_briefing_chars": self.max_briefing_chars, "critic": critic}


def signals_of(view: Any, policy: GatePolicy) -> dict[str, Any]:
    """FR-102: the rule signals of one claim as read, and the values each compared.

    `no_claims` belongs to a node, not a claim, and is decided by the gate."""
    supporting, contradicting = len(set(view.supporting)), len(set(view.contradicting))
    # The critic's own contributions are excluded: they carry no confidence (FR-105).
    confidences = [c.confidence for c in view.contributions
                   if not (c.node_id or "").endswith(CRITIC_NODE_SUFFIX)]
    lowest = None
    if confidences:
        lowest = None if any(c is None for c in confidences) else min(confidences)
    rules = {
        "unsupported": supporting == 0,
        "contradicted": contradicting > 0,
        "thin_support": 1 <= supporting < policy.min_sources,
        # A claim only a critic contributed to has no stated confidence: below (FR-110).
        "low_confidence": bool(view.contributions) and (lowest is None or lowest < policy.confidence_threshold),
    }
    return {
        "claim": view.claim.uri,
        "fired": [name for name in SIGNALS if rules.get(name) and name in policy.signals],
        "values": {"supporting": supporting, "contradicting": contradicting, "min_sources": policy.min_sources,
                   "lowest_confidence": lowest, "confidence_threshold": policy.confidence_threshold},
    }
