"""The calibration report (FR-107, M28): the gate's signals and threshold against external
outcomes, computed from the record and never applied (P3-D19).

Its unit is a claim judged by a gate, at the time of that gate: each gate row's judged
claims, with the signals and lowest confidence that row recorded, against the claim's
latest external outcome. A claim no gate judged is not counted. Critic verdicts are a
lower-confidence signal, reported beside the rest and never used in the suggestion
(ADR-15). Nothing here writes: the report is a value, and the threshold it suggests is
only ever printed, so the manual default stands until someone changes it (I-23).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .gate import OUTCOMES, SIGNALS

# A claim's signals. `no_claims` fires on a node, never on a claim, so it is counted by
# gate row as `no_claims_fired` instead (FR-111, M28 review).
_CLAIM_SIGNALS = tuple(s for s in SIGNALS if s != "no_claims")

__all__ = ["THRESHOLDS", "CalibrationReport", "calibrate"]

# FR-107: k/20 for k from 1 to 19.
THRESHOLDS = tuple(k / 20 for k in range(1, 20))
_COLUMNS = ("correct", "incorrect", "no_outcome")


def _counts() -> dict[str, int]:
    return dict.fromkeys(_COLUMNS, 0)


@dataclass(frozen=True)
class CalibrationReport:
    """FR-107: what the gate's rules would have caught, against what was found to be so.

    `judged` splits the counted claims by outcome; `signals` maps each signal to its
    `fired` and `quiet` counts, so split; `thresholds` gives, at each value, the incorrect
    claims a threshold there would have flagged and the correct ones it would have flagged
    needlessly; `critic` maps each verdict to the outcomes of the claims it was given on,
    counting a critic row only when its own node execution's gate row was read;
    `no_claims_fired` counts the gate rows that fired `no_claims`.
    `outcomes` names every external verification read, counted or not, with the range of
    their times; `claims_with_outcome` is what `min_outcomes` is compared with."""

    tenant_id: str
    project_id: str
    since: datetime | None
    min_outcomes: int
    judged: Mapping[str, int]
    claims_with_outcome: int
    signals: Mapping[str, Mapping[str, Mapping[str, int]]]
    no_claims_fired: int
    thresholds: tuple[Mapping[str, Any], ...]
    suggested_threshold: float | None
    critic: Mapping[str, Mapping[str, int]]
    outcomes: tuple[str, ...]
    outcomes_from: datetime | None
    outcomes_to: datetime | None

    def to_json(self) -> dict[str, Any]:
        def at(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "tenant_id": self.tenant_id, "project_id": self.project_id, "since": at(self.since),
            "min_outcomes": self.min_outcomes, "judged": dict(self.judged),
            "claims_with_outcome": self.claims_with_outcome,
            "signals": {s: {k: dict(v) for k, v in split.items()} for s, split in self.signals.items()},
            "no_claims_fired": self.no_claims_fired,
            "thresholds": [dict(t) for t in self.thresholds], "suggested_threshold": self.suggested_threshold,
            "critic": {verdict: dict(split) for verdict, split in self.critic.items()},
            "outcomes": list(self.outcomes), "outcomes_from": at(self.outcomes_from),
            "outcomes_to": at(self.outcomes_to),
        }


def _checked(since: Any, min_outcomes: Any) -> None:
    if type(min_outcomes) is not int or min_outcomes < 0:
        raise ValueError(f"min_outcomes must be an int of 0 or more, got {min_outcomes!r}")
    if since is not None and (not isinstance(since, datetime) or since.utcoffset() is None):
        raise ValueError(f"since must be a timezone-aware datetime or None, got {since!r}")


def calibrate(rows: Iterable[Any], *, tenant_id: str, project_id: str, since: datetime | None,
              min_outcomes: int) -> CalibrationReport:
    """FR-107 over verifications already read, in any order. The store has read them
    within one tenant and project (NFR-25); this only computes."""
    _checked(since, min_outcomes)
    rows = list(rows)
    gates = [r for r in rows if r.kind == "gate"]
    externals = sorted((r for r in rows if r.kind == "external"), key=lambda r: r.created_at)
    # A claim's latest outcome. Two at the same instant, which the stores order
    # differently, read as incorrect: the cautious reading, the same on both.
    latest: dict[str, tuple[datetime, str]] = {}
    for r in externals:
        if r.claim_id is None:
            continue  # a node's outcome: read and named, but the unit is a claim
        seen = latest.get(r.subject)
        if seen is None or r.created_at > seen[0] or (r.created_at == seen[0] and r.outcome == "incorrect"):
            latest[r.subject] = (r.created_at, r.outcome)

    def outcome_of(claim: str) -> str:
        return latest[claim][1] if claim in latest else "no_outcome"

    judged = _counts()
    signals = {s: {"fired": _counts(), "quiet": _counts()} for s in _CLAIM_SIGNALS}
    units: list[tuple[str, float | None]] = []
    claims: set[str] = set()
    for gate in gates:
        for item in gate.detail.get("claims", ()):
            claim = item["claim"]
            outcome = outcome_of(claim)
            claims.add(claim)
            judged[outcome] += 1
            fired = set(item.get("fired", ()))
            for s in _CLAIM_SIGNALS:
                signals[s]["fired" if s in fired else "quiet"][outcome] += 1
            units.append((outcome, item.get("values", {}).get("lowest_confidence")))

    def flags(confidence: float | None, threshold: float) -> bool:
        return confidence is None or confidence < threshold

    thresholds = tuple(
        {"threshold": t,
         "incorrect_flagged": sum(o == "incorrect" and flags(c, t) for o, c in units),
         "correct_flagged": sum(o == "correct" and flags(c, t) for o, c in units)}
        for t in THRESHOLDS)
    incorrect = judged["incorrect"]
    with_outcome = sum(c in latest for c in claims)
    suggested = None
    if with_outcome >= min_outcomes and incorrect:
        suggested = next((t["threshold"] for t in thresholds if t["incorrect_flagged"] == incorrect), None)

    no_claims_fired = sum("no_claims" in (g.detail.get("fired") or ()) for g in gates)

    # A critic row on a claim counts only when its own node execution's gate row was read:
    # the critic's node row, with the same critic_run, names that execution (FR-111). A
    # claim shared by several nodes is never counted against another node's gate.
    gated = {g.subject for g in gates}
    executions = {r.detail.get("critic_run"): r.subject for r in rows
                  if r.kind == "critic" and r.claim_id is None}
    critic = {verdict: _counts() for verdict in OUTCOMES["critic"]}
    for r in rows:
        if (r.kind == "critic" and r.claim_id is not None
                and executions.get(r.detail.get("critic_run")) in gated):
            critic[r.outcome][outcome_of(r.subject)] += 1

    return CalibrationReport(
        tenant_id=tenant_id, project_id=project_id, since=since, min_outcomes=min_outcomes, judged=judged,
        claims_with_outcome=with_outcome, signals=signals, no_claims_fired=no_claims_fired,
        thresholds=thresholds, suggested_threshold=suggested,
        critic=critic, outcomes=tuple(r.uri for r in externals),
        outcomes_from=externals[0].created_at if externals else None,
        outcomes_to=externals[-1].created_at if externals else None,
    )
