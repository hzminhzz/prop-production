"""Missed-opening memory is scoped to one immutable owner and instrument."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from .clocks import CheckpointMs, DecisionMs
from .money import FixedKSizing, NativeInstrument, decimal


@dataclass(frozen=True, slots=True)
class BaselineHeld:
    baseline_qty: Decimal


@dataclass(frozen=True, slots=True)
class OpeningMissed:
    missed_at: CheckpointMs
    qty_at_miss: Decimal

    def allows_retry(self, quantity: Decimal) -> bool:
        if not quantity:
            return False
        return (quantity > 0) != (self.qty_at_miss > 0) or (
            quantity.copy_abs() > self.qty_at_miss.copy_abs()
        )


@dataclass(frozen=True, slots=True)
class LiveTracked:
    committed_source_qty: Decimal


type InstrumentEntryState = BaselineHeld | OpeningMissed | LiveTracked


@dataclass(frozen=True, slots=True)
class CopyMandateSpec:
    mandate_id: str
    generation: int
    source_id: str
    source_scope_generation: str
    source_account_sha: str
    account_id: str
    seat_id: str
    sizing: FixedKSizing
    k_wire: str
    allowed_markets: tuple[NativeInstrument, ...]
    policy_epoch: str
    risk_policy_sha: str
    rulebook_sha: str
    effective_from_ms: DecisionMs
    effective_until_ms: DecisionMs

    @classmethod
    def parse(cls, value: dict[str, Any]) -> CopyMandateSpec:
        return parse_mandate(value)

    @property
    def data(self) -> dict[str, Any]:
        return {
            "schema": "copy_mandate_v1",
            "mandate_id": self.mandate_id,
            "generation": self.generation,
            "source_id": self.source_id,
            "source_scope_generation": self.source_scope_generation,
            "source_account_sha": self.source_account_sha,
            "account_id": self.account_id,
            "pod_id": self.seat_id,
            "k": self.k_wire,
            "allowed_markets": [instrument.key for instrument in self.allowed_markets],
            "policy_epoch": self.policy_epoch,
            "risk_policy_sha": self.risk_policy_sha,
            "rulebook_sha": self.rulebook_sha,
            "effective_from_ms": self.effective_from_ms.value,
            "effective_until_ms": self.effective_until_ms.value,
            "sizing_policy": "FIXED_K_NATIVE_QUANTITY",
            "entry_policy": "ONE_SHOT_CHECKPOINT_V3",
            "exit_policy": "NATIVE_SAFE_CROSS_OR_REDUCE_V3",
        }


def _sha(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("INVALID_SHA256")


def _integer(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("CWSA_CLOCK_OR_CURSOR")
    return value


def _fields(value: object, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("CWSA_FIELDS")


def parse_mandate(value: dict[str, Any]) -> CopyMandateSpec:
    _fields(
        value,
        {
            "schema",
            "mandate_id",
            "generation",
            "source_id",
            "source_scope_generation",
            "source_account_sha",
            "account_id",
            "pod_id",
            "k",
            "allowed_markets",
            "policy_epoch",
            "risk_policy_sha",
            "rulebook_sha",
            "effective_from_ms",
            "effective_until_ms",
            "sizing_policy",
            "entry_policy",
            "exit_policy",
        },
    )
    if (
        value["schema"] != "copy_mandate_v1"
        or value["sizing_policy"] != "FIXED_K_NATIVE_QUANTITY"
        or value["entry_policy"] != "ONE_SHOT_CHECKPOINT_V3"
        or value["exit_policy"] != "NATIVE_SAFE_CROSS_OR_REDUCE_V3"
    ):
        raise ValueError("MANDATE_CONTRACT")
    for key in ("mandate_id", "source_id", "account_id", "pod_id"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 100:
            raise ValueError("MANDATE_IDENTITY")
    for key in (
        "source_scope_generation",
        "source_account_sha",
        "policy_epoch",
        "risk_policy_sha",
        "rulebook_sha",
    ):
        _sha(value[key])
    if (
        _integer(value["generation"]) == 0
        or not isinstance(value["k"], str)
        or len(value["k"]) > 128
        or not 0 < decimal(value["k"]) <= 10
        or _integer(value["effective_until_ms"]) <= _integer(value["effective_from_ms"])
    ):
        raise ValueError("MANDATE_GENERATION_K_OR_CLOCK")
    markets = value["allowed_markets"]
    if not isinstance(markets, list) or not markets:
        raise ValueError("MANDATE_MARKETS")
    if any(not NativeInstrument.valid(key) for key in markets):
        raise ValueError("CWSA_NATIVE_KEY_OR_QUANTITY")
    if len(set(markets)) != len(markets):
        raise ValueError("CWSA_ZERO_DUPLICATE_OR_RANGE")
    if markets != sorted(markets):
        raise ValueError("CWSA_VECTOR_ORDER")
    return CopyMandateSpec(
        value["mandate_id"],
        value["generation"],
        value["source_id"],
        value["source_scope_generation"],
        value["source_account_sha"],
        value["account_id"],
        value["pod_id"],
        FixedKSizing(decimal(value["k"])),
        value["k"],
        tuple(NativeInstrument(key) for key in markets),
        value["policy_epoch"],
        value["risk_policy_sha"],
        value["rulebook_sha"],
        DecisionMs(value["effective_from_ms"]),
        DecisionMs(value["effective_until_ms"]),
    )


class MandateIdentity(Protocol):
    @property
    def sha(self) -> str: ...


class MandatePhase(StrEnum):
    ABSENT = "ABSENT"
    AWAITING_BASELINE = "AWAITING_BASELINE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"


class MandateEvent(StrEnum):
    ACTIVATE = "ACTIVATE"
    COMPLETE_BASELINE = "COMPLETE_BASELINE"
    PAUSE = "PAUSE"
    RETIRE = "RETIRE"


@dataclass(frozen=True, slots=True)
class MandateState:
    phase: MandatePhase
    mandate_sha: str
    sequence: int
    activated_ms: int | None = None
    baseline_outcome_id: str | None = None

    @classmethod
    def initial(cls, mandate: str | MandateIdentity) -> MandateState:
        return cls(MandatePhase.ABSENT, mandate if isinstance(mandate, str) else mandate.sha, 0)

    @classmethod
    def parse(cls, value: dict[str, Any]) -> MandateState:
        _fields(value, {"phase", "mandate_sha", "sequence", "activated_ms", "baseline_outcome_id"})
        _sha(value["mandate_sha"])
        _integer(value["sequence"])
        if value["activated_ms"] is not None:
            _integer(value["activated_ms"])
        if value["baseline_outcome_id"] is not None:
            _sha(value["baseline_outcome_id"])
        return cls(
            MandatePhase(value["phase"]),
            value["mandate_sha"],
            value["sequence"],
            value["activated_ms"],
            value["baseline_outcome_id"],
        )

    @property
    def data(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "mandate_sha": self.mandate_sha,
            "sequence": self.sequence,
            "activated_ms": self.activated_ms,
            "baseline_outcome_id": self.baseline_outcome_id,
        }


MandateGuard = Literal[
    "PROSPECTIVE_ACTIVATION",
    "POST_ACTIVATION_COMPLETE",
    "AUTHORIZED_STOP",
    "ILLEGAL_EDGE",
    "INVALID_CLOCK",
]
MandateEffect = Literal[
    "AWAIT_BASELINE", "BASELINE_ONLY", "STOP_NEW_RISK_PRESERVE_OBLIGATIONS", "REJECTED"
]


@dataclass(frozen=True, slots=True)
class MandateTransition:
    predecessor: MandateState
    event: MandateEvent
    evidence_sha: str
    at_ms: int
    guard: MandateGuard
    successor: MandateState
    effect: MandateEffect
    checkpoint_ms: int | None = None

    @property
    def data(self) -> dict[str, Any]:
        return {
            "schema": "copy_mandate_transition_v1",
            "predecessor": self.predecessor.data,
            "event": self.event.value,
            "evidence_sha": self.evidence_sha,
            "actor": "SOURCE_CONSUMER"
            if self.event == MandateEvent.COMPLETE_BASELINE
            else "RELEASE_AUTHORITY",
            "at_ms": self.at_ms,
            "guard": self.guard,
            "successor": self.successor.data,
            "effect": self.effect,
            "checkpoint_ms": self.checkpoint_ms,
        }


def transition_mandate(
    mandate: CopyMandateSpec,
    before: MandateState,
    *,
    mandate_sha: str,
    event: MandateEvent,
    evidence_sha: str,
    at_ms: int,
    checkpoint_ms: int | None = None,
) -> MandateTransition:
    _integer(at_ms)
    _sha(evidence_sha)
    if before.mandate_sha != mandate_sha:
        raise ValueError("MANDATE_PREDECESSOR")
    body = dict(before.data, sequence=before.sequence + 1)
    guard: MandateGuard = "ILLEGAL_EDGE"
    effect: MandateEffect = "REJECTED"
    if event == MandateEvent.ACTIVATE and before.phase == MandatePhase.ABSENT:
        if at_ms <= mandate.effective_from_ms.value:
            body.update(phase="AWAITING_BASELINE", activated_ms=at_ms)
            guard, effect = "PROSPECTIVE_ACTIVATION", "AWAIT_BASELINE"
        else:
            guard = "INVALID_CLOCK"
    elif event == MandateEvent.COMPLETE_BASELINE and before.phase == MandatePhase.AWAITING_BASELINE:
        if (
            type(checkpoint_ms) is int
            and mandate.effective_from_ms.value <= checkpoint_ms <= at_ms
            and at_ms < mandate.effective_until_ms.value
        ):
            body.update(phase="ACTIVE", baseline_outcome_id=evidence_sha)
            guard, effect = "POST_ACTIVATION_COMPLETE", "BASELINE_ONLY"
        else:
            guard = "INVALID_CLOCK"
    elif (
        event == MandateEvent.PAUSE
        and before.phase in (MandatePhase.AWAITING_BASELINE, MandatePhase.ACTIVE)
    ) or (
        event == MandateEvent.RETIRE
        and before.phase
        in (MandatePhase.AWAITING_BASELINE, MandatePhase.ACTIVE, MandatePhase.PAUSED)
    ):
        if before.activated_ms is not None and at_ms >= before.activated_ms:
            body["phase"] = "PAUSED" if event == MandateEvent.PAUSE else "RETIRED"
            guard, effect = "AUTHORIZED_STOP", "STOP_NEW_RISK_PRESERVE_OBLIGATIONS"
        else:
            guard = "INVALID_CLOCK"
    return MandateTransition(
        before, event, evidence_sha, at_ms, guard, MandateState.parse(body), effect, checkpoint_ms
    )
