"""Immutable signed mandate contracts; the existing Guardian owns durable state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .checkpoint_source import fields, integer, parse_vector, validate_pins, verified_outcome
from .contracts import PORTFOLIO_RELEASE_V1, PORTFOLIO_RELEASE_V2
from .evidence import canonical, decimal, digest, load_json, require_sha, verify


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
        parse_vector([{"instrument": market, "quantity": "1"} for market in markets])
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
        k: (PORTFOLIO_RELEASE_V1 if k == "schema" else value)
        for k, value in payload.items()
        if k not in extra
    }


def mandate_authority(
    mandate_key: Ed25519PublicKey | None,
    release_payload: dict[str, Any],
    *,
    signed: bool = True,
) -> Ed25519PublicKey:
    if (
        mandate_key is None
        or release_payload["schema"] != PORTFOLIO_RELEASE_V2
        or not signed
        or sha256(mandate_key.public_bytes_raw()).hexdigest()
        != release_payload["mandate_authority_sha"]
    ):
        raise ValueError("CHECKPOINT_MANDATE_AUTHORITY_NOT_PINNED")
    return mandate_key


def prepare_activate_mandate(
    state: dict[str, Any],
    *,
    mandate_key: Ed25519PublicKey | None,
    source_key: Ed25519PublicKey,
    release_payload: dict[str, Any],
    signed_release: bytes | None,
    release_sha: str,
    policy: dict[str, Any],
    markets: dict[str, Any],
    envelope: dict[str, Any],
    scope_reasons: list[str],
    now_ms: int,
) -> tuple[MandateState, str | None, dict[str, Any] | None]:
    key = mandate_authority(mandate_key, release_payload, signed=signed_release is not None)
    mandate = CopyMandate.parse(verify(envelope, key, "copy-mandate-v1"))
    m = mandate.data
    if scope_reasons or m not in release_payload["mandates"]:
        raise ValueError("MANDATE_RELEASE_SCOPE")
    records = state.get("copy_mandates", {})
    existing = records.get(mandate.owner)
    if existing:
        if existing["mandate"] != m:
            raise ValueError("MANDATE_GENERATION_EQUIVOCATION")
        return MandateState.parse(existing["state"]), None, None
    for old in records.values():
        p = old["mandate"]
        if p["mandate_id"] == m["mandate_id"] and p["generation"] >= m["generation"]:
            raise ValueError("MANDATE_GENERATION_ROLLBACK")
        if (
            p["pod_id"] == m["pod_id"]
            and old["state"]["phase"] in {"ACTIVE", "AWAITING_BASELINE"}
            and max(p["effective_from_ms"], m["effective_from_ms"])
            < min(p["effective_until_ms"], m["effective_until_ms"])
        ):
            raise ValueError("MANDATE_OPENING_INTERVAL_OVERLAP")
    transition = transition_mandate(
        mandate,
        MandateState.initial(mandate),
        event=MandateEvent.ACTIVATE,
        evidence_sha=mandate.sha,
        at_ms=now_ms,
    )
    if transition.effect == "REJECTED":
        raise ValueError("MANDATE_ACTIVATION_NOT_PROSPECTIVE")
    assert signed_release is not None
    record = {
        "mandate": m,
        "approval": envelope,
        "authority_public": key.public_bytes_raw().hex(),
        "release_envelope": load_json(signed_release),
        "release_sha": release_sha,
        "source_public": source_key.public_bytes_raw().hex(),
        "risk_contract": {"policy": policy, "markets": markets},
        "state": transition.successor.data,
        "history": [transition.data],
        "control_envelopes": {},
    }
    return transition.successor, mandate.owner, record


def prepare_control_mandate(
    state: dict[str, Any],
    *,
    mandate_key: Ed25519PublicKey | None,
    release_payload: dict[str, Any],
    has_signed_release: bool,
    envelope: dict[str, Any],
    now_ms: int,
) -> tuple[MandateState, dict[str, Any] | None]:
    key = mandate_authority(mandate_key, release_payload, signed=has_signed_release)
    command = verify(envelope, key, "copy-mandate-control-v1")
    fields(
        command,
        {
            "schema",
            "owner",
            "mandate_sha",
            "predecessor_sha",
            "event",
            "issued_ms",
            "expires_ms",
            "nonce",
        },
    )
    integer(now_ms)
    if (
        command["schema"] != "copy_mandate_control_v1"
        or command["event"] not in {"PAUSE", "RETIRE"}
        or not integer(command["issued_ms"]) <= now_ms < integer(command["expires_ms"])
    ):
        raise ValueError("MANDATE_CONTROL_SCHEMA_OR_CLOCK")
    for field in ("owner", "mandate_sha", "predecessor_sha", "nonce"):
        require_sha(command[field])
    command_sha = digest(command)
    replay = state.get("mandate_control_nonces", {}).get(command["nonce"])
    if replay:
        if replay["command_sha"] != command_sha:
            raise ValueError("MANDATE_CONTROL_NONCE_CONFLICT")
        return MandateState.parse(replay["result"]), None
    entry = state["copy_mandates"][command["owner"]]
    mandate, before = CopyMandate.parse(entry["mandate"]), MandateState.parse(entry["state"])
    if mandate.sha != command["mandate_sha"] or before.sha != command["predecessor_sha"]:
        raise ValueError("MANDATE_CONTROL_PREDECESSOR")
    transition = transition_mandate(
        mandate,
        before,
        event=MandateEvent(command["event"]),
        evidence_sha=command_sha,
        at_ms=now_ms,
    )
    update = {
        "owner": mandate.owner,
        "state": transition.successor.data,
        "transition": transition.data,
        "command_sha": command_sha,
        "nonce": command["nonce"],
    }
    return transition.successor, update


def prepare_baseline_mandate(
    entry: dict[str, Any],
    *,
    outcomes: list[dict[str, Any]],
    packet_for: Any,
    now_ms: int,
) -> tuple[MandateState, dict[str, Any] | None]:
    mandate, before = CopyMandate.parse(entry["mandate"]), MandateState.parse(entry["state"])
    if before.phase != "AWAITING_BASELINE":
        return before, None
    m = mandate.data
    candidates = [
        record
        for record in outcomes
        if record["outcome"]["source_id"] == m["source_id"]
        and record["outcome"]["scope_generation"] == m["source_scope_generation"]
        and record["outcome"]["policy_epoch"] == m["policy_epoch"]
        and record["outcome"]["sample"] is not None
        and record["outcome"]["sample"]["scope"]["account_sha"] == m["source_account_sha"]
        and record["outcome"]["kind"] in {"BASELINE", "UNCHANGED", "CHANGED", "RECOVERY_BASELINE"}
        and m["effective_from_ms"] <= record["outcome"]["checkpoint_ms"]
        and record["outcome"]["published_ms"] <= now_ms
    ]
    if not candidates:
        return before, None
    proof = min(candidates, key=lambda item: item["outcome"]["checkpoint_ms"])
    transition = transition_mandate(
        mandate,
        before,
        event=MandateEvent.COMPLETE_BASELINE,
        evidence_sha=proof["outcome"]["outcome_id"],
        at_ms=now_ms,
        checkpoint_ms=proof["outcome"]["checkpoint_ms"],
    )
    update = {
        "state": transition.successor.data,
        "transition": transition.data,
        "baseline_proof": proof,
        "baseline_envelope": packet_for(proof["outcome"]["outcome_id"]),
    }
    return transition.successor, update


def validate_mandate_history(previous: dict[str, Any], current: dict[str, Any], kind: str) -> None:
    public = current.get("checkpoint_source_public")
    if previous.get("checkpoint_source_public") is not None:
        if public != previous["checkpoint_source_public"]:
            raise ValueError("MANDATE_SOURCE_AUTHORITY_REWRITTEN")
    elif public is not None and kind != "BIND_RELEASE_DISARMED":
        raise ValueError("MANDATE_SOURCE_AUTHORITY_UNBOUND")
    old, new = previous.get("copy_mandates", {}), current.get("copy_mandates", {})
    if not set(old).issubset(new):
        raise ValueError("MANDATE_HISTORY_REMOVED")
    for owner, entry in new.items():
        mandate = CopyMandate.parse(entry["mandate"])
        if owner != mandate.owner or not entry["history"]:
            raise ValueError("MANDATE_OWNER_OR_HISTORY")
        if owner in old:
            for field in (
                "mandate",
                "approval",
                "authority_public",
                "release_envelope",
                "release_sha",
                "source_public",
                "risk_contract",
            ):
                if old[owner][field] != entry[field]:
                    raise ValueError("MANDATE_IMMUTABLE_GENERATION_REWRITTEN")
            if old[owner].get("baseline_proof") is not None and any(
                old[owner].get(k) != entry.get(k) for k in ("baseline_proof", "baseline_envelope")
            ):
                raise ValueError("MANDATE_BASELINE_REWRITTEN")
            if entry["history"][: len(old[owner]["history"])] != old[owner]["history"]:
                raise ValueError("MANDATE_TRANSITIONS_REWRITTEN")
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(entry["authority_public"]))
        if verify(entry["approval"], key, "copy-mandate-v1") != mandate.data:
            raise ValueError("MANDATE_APPROVAL_DRIFT")
        release = verify(entry["release_envelope"], key, entry["release_envelope"]["authority"])
        if (
            digest(release) != entry["release_sha"]
            or release["mandate_authority_sha"] != sha256(key.public_bytes_raw()).hexdigest()
            or mandate.data not in release["mandates"]
            or entry["source_public"] != public
            or digest(entry["risk_contract"]) != mandate.data["risk_policy_sha"]
        ):
            raise ValueError("MANDATE_RELEASE_BINDING")
        if owner not in old and (
            kind != "COPY_MANDATE_ACTIVATE"
            or entry["release_sha"] != current.get("active_release_sha")
            or any(
                prior["mandate"]["mandate_id"] == mandate.data["mandate_id"]
                and prior["mandate"]["generation"] >= mandate.data["generation"]
                for prior in old.values()
            )
        ):
            raise ValueError("MANDATE_ACTIVATION_RELEASE_OR_GENERATION")
        before = MandateState.initial(mandate)
        for transition in entry["history"]:
            expected = transition_mandate(
                mandate,
                before,
                event=MandateEvent(transition["event"]),
                evidence_sha=transition["evidence_sha"],
                at_ms=transition["at_ms"],
                checkpoint_ms=transition["checkpoint_ms"],
            )
            if canonical(expected.data) != canonical(transition):
                raise ValueError("MANDATE_TRANSITION_REPLAY")
            if transition["event"] == "COMPLETE_BASELINE":
                outcome = verified_outcome(
                    entry["baseline_envelope"],
                    Ed25519PublicKey.from_public_bytes(bytes.fromhex(entry["source_public"])),
                    release["checkpoint_scopes"][mandate.data["source_id"]],
                    release["checkpoint_policy"],
                    transition["evidence_sha"],
                )
                if (
                    outcome != entry["baseline_proof"]["outcome"]
                    or outcome["kind"]
                    not in {"BASELINE", "UNCHANGED", "CHANGED", "RECOVERY_BASELINE"}
                    or outcome["checkpoint_ms"] != transition["checkpoint_ms"]
                    or outcome["published_ms"] > transition["at_ms"]
                ):
                    raise ValueError("MANDATE_BASELINE_PROOF")
            if transition["event"] in {"PAUSE", "RETIRE"}:
                command = verify(
                    entry["control_envelopes"][transition["evidence_sha"]],
                    key,
                    "copy-mandate-control-v1",
                )
                if (
                    digest(command) != transition["evidence_sha"]
                    or command["predecessor_sha"] != before.sha
                    or command["mandate_sha"] != mandate.sha
                    or command["event"] != transition["event"]
                    or not command["issued_ms"] <= transition["at_ms"] < command["expires_ms"]
                ):
                    raise ValueError("MANDATE_CONTROL_REPLAY")
            before = expected.successor
        if entry["state"] != before.data:
            raise ValueError("MANDATE_STATE_REPLAY")
    opening = [r for r in new.values() if r["state"]["phase"] in {"ACTIVE", "AWAITING_BASELINE"}]
    for index, entry in enumerate(opening):
        m = entry["mandate"]
        for other in opening[index + 1 :]:
            n = other["mandate"]
            if (m["account_id"], m["pod_id"]) == (n["account_id"], n["pod_id"]) and max(
                m["effective_from_ms"], n["effective_from_ms"]
            ) < min(m["effective_until_ms"], n["effective_until_ms"]):
                raise ValueError("MANDATE_OPENING_INTERVAL_OVERLAP")
    if kind.startswith("COPY_MANDATE") and any(
        previous.get(field) != current.get(field)
        for field in (
            "finance",
            "positions",
            "pod_positions",
            "fills",
            "intents",
            "reservations",
            "pod_fees_paid",
            "pod_realized",
            "pod_loss_used",
            "checkpoint_basis",
            "checkpoint_allocations",
            "checkpoint_internal_effects",
            "checkpoint_market_claims",
            "halts",
        )
    ):
        raise ValueError("MANDATE_TRANSITION_MUTATED_FINANCIAL_OBLIGATIONS")
