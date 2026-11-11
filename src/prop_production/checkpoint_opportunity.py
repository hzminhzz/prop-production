"""Independent protected implementation of the canonical one-shot protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from .checkpoint_source import fields, integer
from .evidence import require_sha


class OpportunityPhase(StrEnum):
    UNDECIDED = "UNDECIDED"
    REVIEWABLE = "REVIEWABLE"
    PRE_DISPATCH_BLOCKED = "PRE_DISPATCH_BLOCKED"
    COMMITTED_ONCE = "COMMITTED_ONCE"
    MISSED = "MISSED"


class OpportunityEvent(StrEnum):
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"
    COMMIT = "COMMIT"
    MISS = "MISS"


@dataclass(frozen=True)
class OpportunityState:
    opportunity_id: str
    owner: str
    signal_id: str
    deadline_ms: int
    phase: OpportunityPhase = OpportunityPhase.UNDECIDED
    sequence: int = 0
    last_at_ms: int = 0

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self) | {"phase": self.phase.value}

    @classmethod
    def parse(cls, value: dict[str, Any]) -> OpportunityState:
        fields(
            value,
            {
                "opportunity_id",
                "owner",
                "signal_id",
                "deadline_ms",
                "phase",
                "sequence",
                "last_at_ms",
            },
        )
        for key in ("opportunity_id", "owner", "signal_id"):
            require_sha(value[key])
        return cls(
            value["opportunity_id"],
            value["owner"],
            value["signal_id"],
            integer(value["deadline_ms"]),
            OpportunityPhase(value["phase"]),
            integer(value["sequence"]),
            integer(value["last_at_ms"]),
        )


@dataclass(frozen=True)
class OpportunityTransition:
    predecessor: OpportunityState
    event: OpportunityEvent
    evidence_sha: str
    at_ms: int
    actor: str
    guard: str
    successor: OpportunityState
    effect: str

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self) | {
            "event": self.event.value,
            "predecessor": self.predecessor.data,
            "successor": self.successor.data,
        }


def advance(
    before: OpportunityState,
    *,
    event: OpportunityEvent,
    evidence_sha: str,
    at_ms: int,
    guard: str,
    no_side_effect: bool = False,
) -> OpportunityTransition:
    require_sha(evidence_sha)
    integer(at_ms)
    if not isinstance(guard, str) or not guard:
        raise ValueError("OPPORTUNITY_GUARD")
    if before.phase in (OpportunityPhase.COMMITTED_ONCE, OpportunityPhase.MISSED):
        raise ValueError("OPPORTUNITY_TERMINAL")
    if at_ms < before.last_at_ms:
        raise ValueError("OPPORTUNITY_CLOCK_REGRESSION")
    if event != OpportunityEvent.MISS and at_ms >= before.deadline_ms:
        raise ValueError("OPPORTUNITY_EXPIRED")
    if event == OpportunityEvent.REVIEW:
        if (
            before.phase not in (OpportunityPhase.UNDECIDED, OpportunityPhase.PRE_DISPATCH_BLOCKED)
            or not no_side_effect
        ):
            raise ValueError("OPPORTUNITY_REVIEW_REQUIRES_NO_SIDE_EFFECT")
        phase, effect = OpportunityPhase.REVIEWABLE, "REVIEW_ONLY"
    elif event == OpportunityEvent.BLOCK:
        if before.phase != OpportunityPhase.REVIEWABLE or not no_side_effect:
            raise ValueError("OPPORTUNITY_BLOCK_REQUIRES_NO_SIDE_EFFECT")
        phase, effect = OpportunityPhase.PRE_DISPATCH_BLOCKED, "NO_SIDE_EFFECT"
    elif event == OpportunityEvent.COMMIT:
        if before.phase != OpportunityPhase.REVIEWABLE:
            raise ValueError("OPPORTUNITY_COMMIT_PREDECESSOR")
        phase, effect = OpportunityPhase.COMMITTED_ONCE, "COMMITTED_ONCE"
    else:
        phase, effect = OpportunityPhase.MISSED, "MISSED"
    after = replace(before, phase=phase, sequence=before.sequence + 1, last_at_ms=at_ms)
    return OpportunityTransition(
        before, event, evidence_sha, at_ms, "GUARDIAN", guard, after, effect
    )
