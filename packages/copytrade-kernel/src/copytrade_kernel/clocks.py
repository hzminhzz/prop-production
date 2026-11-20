"""Explicit decision clocks and source provenance. Callers supply time."""

from dataclasses import dataclass

GRID_MS = 300_000


def clock(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("INVALID_CLOCK")


@dataclass(frozen=True, slots=True, order=True)
class EventTs:
    value: int

    def __post_init__(self) -> None:
        clock(self.value)


@dataclass(frozen=True, slots=True, order=True)
class KnownAtTs:
    value: int

    def __post_init__(self) -> None:
        clock(self.value)


@dataclass(frozen=True, slots=True, order=True)
class CheckpointMs:
    value: int

    def __post_init__(self) -> None:
        clock(self.value)
        if self.value % GRID_MS:
            raise ValueError("INVALID_CHECKPOINT_GRID")


@dataclass(frozen=True, slots=True, order=True)
class DecisionMs:
    value: int

    def __post_init__(self) -> None:
        clock(self.value)


@dataclass(frozen=True, slots=True)
class BiTemporalStamp:
    event_ts: EventTs
    first_known_at: KnownAtTs

    def __post_init__(self) -> None:
        if self.event_ts.value > self.first_known_at.value:
            raise ValueError("INVALID_KNOWLEDGE_CLOCK")


@dataclass(frozen=True, slots=True)
class ProspectiveCWSA:
    source_cursor: int
    chain_sha: str
    packet_sha: str


@dataclass(frozen=True, slots=True)
class RetrospectiveArchive:
    archive_sha: str
    repaired_from_fills: bool = True


@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: str
    since: KnownAtTs
