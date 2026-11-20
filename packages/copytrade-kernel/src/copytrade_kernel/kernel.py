"""Deterministic checkpoint entry projection for prospective and research frames."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, TypeVar

from .clocks import CheckpointMs, DecisionMs, KnownAtTs, ProspectiveCWSA, RetrospectiveArchive
from .decision import DecisionInputs, ExecutionDecision, plan_execution
from .mandates import OpeningMissed
from .money import FixedKSizing, NativeInstrument

ProvenanceT = TypeVar("ProvenanceT", ProspectiveCWSA, RetrospectiveArchive, covariant=True)
DECISION_SEMANTICS = "checkpoint_decision_q15_v1"
Q15_REJECTION = "Q15_OPENING_RETRY_REQUIRES_INCREASE_OR_FLIP"


@dataclass(frozen=True, slots=True)
class SourceLeg:
    instrument: NativeInstrument
    market: str
    quantity: Decimal
    mark: Decimal


@dataclass(frozen=True, slots=True)
class SourceOutcome(Generic[ProvenanceT]):
    owner: str
    sizing: FixedKSizing
    published_at: KnownAtTs
    deadline: DecisionMs
    legs: tuple[SourceLeg, ...]
    provenance: ProvenanceT


@dataclass(frozen=True, slots=True)
class CheckpointBatch(Generic[ProvenanceT]):
    checkpoint_ms: CheckpointMs
    outcomes: tuple[SourceOutcome[ProvenanceT], ...]


@dataclass(frozen=True, slots=True)
class OwnerBook:
    owner: str
    positions: tuple[tuple[str, Decimal], ...]
    entry_memory: tuple[tuple[NativeInstrument, OpeningMissed], ...]


@dataclass(frozen=True, slots=True)
class KernelState:
    owner_books: tuple[OwnerBook, ...]


@dataclass(frozen=True, slots=True)
class KernelStepResult:
    notionals: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    opening_rejections: tuple[tuple[str, tuple[str, ...]], ...]
    execution: ExecutionDecision | None = None


def step_checkpoint(
    *,
    state: KernelState,
    frame: CheckpointBatch[ProvenanceT],
    inputs: DecisionInputs | None = None,
) -> KernelStepResult:
    """Calculate full fixed-k targets after applying owner-local Q15 memory.

    An opposing retained position may still reduce to flat when a new opening is
    suppressed. An existing same-side position is managed by the risk policy.
    """
    books = {book.owner: book for book in state.owner_books}
    notionals: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    rejections: list[tuple[str, tuple[str, ...]]] = []
    for outcome in frame.outcomes:
        book = books[outcome.owner]
        positions, memory = dict(book.positions), dict(book.entry_memory)
        requested: list[tuple[str, str]] = []
        blocked: list[str] = []
        for leg in outcome.legs:
            miss = memory.get(leg.instrument)
            held = positions.get(leg.market, Decimal(0))
            has_same_side = held and (held > 0) == (leg.quantity > 0)
            if miss is not None and not has_same_side and not miss.allows_retry(leg.quantity):
                blocked.append(leg.instrument.key)
                continue
            requested.append((leg.market, str(leg.quantity * leg.mark * outcome.sizing.k)))
        notionals.append((outcome.owner, tuple(sorted(requested))))
        if blocked:
            rejections.append((outcome.owner, tuple(sorted(blocked))))
    requested_notionals = tuple(sorted(notionals))
    execution = None if inputs is None else plan_execution(requested_notionals, inputs)
    return KernelStepResult(requested_notionals, tuple(sorted(rejections)), execution)


def require_prospective(frame: CheckpointBatch[ProspectiveCWSA]) -> None:
    """Financial shells accept only frames sourced from authenticated CWSA."""
    if any(not isinstance(outcome.provenance, ProspectiveCWSA) for outcome in frame.outcomes):
        raise ValueError("PROSPECTIVE_CWSA_REQUIRED")
