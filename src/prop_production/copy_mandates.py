"""Immutable signed mandate contracts; the existing Guardian owns durable state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from .checkpoint_source import _vector, fields, integer, validate_pins
from .evidence import canonical, decimal, digest, load_json, require_sha


@dataclass(frozen=True)
class CopyMandate:
    canonical_payload: bytes

    @classmethod
    def parse(cls, value: dict[str, Any]) -> CopyMandate:
        fields(
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
            require_sha(value[key])
        if (
            integer(value["generation"]) == 0
            or not isinstance(value["k"], str)
            or len(value["k"]) > 128
            or not 0 < decimal(value["k"]) <= 10
            or integer(value["effective_until_ms"]) <= integer(value["effective_from_ms"])
        ):
            raise ValueError("MANDATE_GENERATION_K_OR_CLOCK")
        markets = value["allowed_markets"]
        if not isinstance(markets, list) or not markets:
            raise ValueError("MANDATE_MARKETS")
        _vector([{"instrument": market, "quantity": "1"} for market in markets])
        return cls(canonical(value))

    @property
    def data(self) -> dict[str, Any]:
        return dict(load_json(self.canonical_payload))

    @property
    def sha(self) -> str:
        return digest(self.data)

    @property
    def owner(self) -> str:
        body = self.data
        return digest([body["account_id"], body["mandate_id"], body["generation"]])


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


@dataclass(frozen=True)
class MandateState:
    phase: MandatePhase
    mandate_sha: str
    sequence: int
    activated_ms: int | None = None
    baseline_outcome_id: str | None = None

    @classmethod
    def initial(cls, mandate: CopyMandate) -> MandateState:
        return cls(MandatePhase.ABSENT, mandate.sha, 0)

    @classmethod
    def parse(cls, value: dict[str, Any]) -> MandateState:
        fields(value, {"phase", "mandate_sha", "sequence", "activated_ms", "baseline_outcome_id"})
        require_sha(value["mandate_sha"])
        integer(value["sequence"])
        if value["activated_ms"] is not None:
            integer(value["activated_ms"])
        if value["baseline_outcome_id"] is not None:
            require_sha(value["baseline_outcome_id"])
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

    @property
    def sha(self) -> str:
        return digest(self.data)


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


@dataclass(frozen=True)
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
    mandate: CopyMandate,
    before: MandateState,
    *,
    event: MandateEvent,
    evidence_sha: str,
    at_ms: int,
    checkpoint_ms: int | None = None,
) -> MandateTransition:
    integer(at_ms)
    require_sha(evidence_sha)
    if before.mandate_sha != mandate.sha:
        raise ValueError("MANDATE_PREDECESSOR")
    m = mandate.data
    body = dict(before.data, sequence=before.sequence + 1)
    guard: MandateGuard = "ILLEGAL_EDGE"
    effect: MandateEffect = "REJECTED"
    if event == MandateEvent.ACTIVATE and before.phase == MandatePhase.ABSENT:
        if at_ms <= m["effective_from_ms"]:
            body.update(phase="AWAITING_BASELINE", activated_ms=at_ms)
            guard, effect = "PROSPECTIVE_ACTIVATION", "AWAIT_BASELINE"
        else:
            guard = "INVALID_CLOCK"
    elif event == MandateEvent.COMPLETE_BASELINE and before.phase == MandatePhase.AWAITING_BASELINE:
        if (
            type(checkpoint_ms) is int
            and m["effective_from_ms"] <= checkpoint_ms <= at_ms
            and at_ms < m["effective_until_ms"]
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


def verify_release_contract(payload: dict[str, Any], authority_public: bytes) -> dict[str, Any]:
    extra = {"checkpoint_policy", "checkpoint_scopes", "mandates", "mandate_authority_sha"}
    if not extra.issubset(payload):
        raise ValueError("MANDATE_RELEASE_FIELDS")
    if payload["mandate_authority_sha"] != sha256(authority_public).hexdigest():
        raise ValueError("MANDATE_AUTHORITY_PIN")
    validate_pins({"policy": payload["checkpoint_policy"], "scopes": payload["checkpoint_scopes"]})
    mandates = payload["mandates"]
    if not isinstance(mandates, list) or not mandates:
        raise ValueError("MANDATE_RELEASE_EMPTY")
    if len(mandates) != len(payload["members"]):
        raise ValueError("MANDATE_RELEASE_MEMBERS")
    seen = set()
    for raw in mandates:
        m = CopyMandate.parse(raw).data
        identity = (m["account_id"], m["pod_id"])
        if identity in seen:
            raise ValueError("MANDATE_RELEASE_OPENING_OVERLAP")
        seen.add(identity)
        source = payload["checkpoint_scopes"].get(m["source_id"])
        if (
            source is None
            or source["generation"] != m["source_scope_generation"]
            or source["account_sha"] != m["source_account_sha"]
            or m["account_id"] != payload["account_binding"]
            or m["policy_epoch"] != digest(payload["checkpoint_policy"])
            or m["risk_policy_sha"] != payload["risk_policy_sha"]
            or m["rulebook_sha"] != payload["rulebook_sha"]
            or m["effective_until_ms"] > payload["expires_at_ms"]
        ):
            raise ValueError("MANDATE_RELEASE_SCOPE")
        member = next((x for x in payload["members"] if x["pod_id"] == m["pod_id"]), None)
        if (
            member is None
            or member["source_identity"] != m["source_id"]
            or member["account_scope"] != [m["source_id"]]
            or member["sizing_policy"] != "FIXED_K_NATIVE_QUANTITY"
            or decimal(member["k"]) != decimal(m["k"])
            or member["allowed_markets"] != m["allowed_markets"]
        ):
            raise ValueError("MANDATE_RELEASE_MEMBER_DRIFT")
    # Existing v1 validation is reused for common provenance/risk/identity
    # fields only. The verified result retains the signed v2 payload.
    return {
        k: ("portfolio_release_v1" if k == "schema" else value)
        for k, value in payload.items()
        if k not in extra
    }
