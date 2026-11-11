"""Checkpoint policy implementation called only by the existing Guardian.

All durable effects use Guardian.journal. This module has no transport or
independent ledger, and never becomes a second account authority.
"""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .checkpoint_source import fields, integer
from .copy_mandates import CopyMandate, MandateEvent, MandateState, transition_mandate
from .evidence import canonical, digest, load_json, require_sha, verify

if TYPE_CHECKING:
    from .guardian import Guardian
    from .v4_source_receiver import SourcePacketReceiver


def _authority(guardian: Guardian) -> Ed25519PublicKey:
    key = guardian.mandate_key
    release = guardian.release
    if (
        key is None
        or release.payload["schema"] != "portfolio_release_v2"
        or release.signed_envelope is None
        or sha256(key.public_bytes_raw()).hexdigest() != release.payload["mandate_authority_sha"]
    ):
        raise ValueError("CHECKPOINT_MANDATE_AUTHORITY_NOT_PINNED")
    return key


def _source(guardian: Guardian, receiver: SourcePacketReceiver) -> None:
    p = guardian.release.payload
    if (
        receiver.wire_version != 3
        or receiver.checkpoint is None
        or receiver.source_key.public_bytes_raw() != guardian.source_key.public_bytes_raw()
        or receiver.release.release_sha != guardian.release.release_sha
        or receiver.checkpoint.pins
        != {"policy": p["checkpoint_policy"], "scopes": p["checkpoint_scopes"]}
    ):
        raise ValueError("CHECKPOINT_SOURCE_AUTHORITY")


def activate_mandate(
    guardian: Guardian,
    envelope: dict[str, Any],
    *,
    now_ms: int,
) -> MandateState:
    key = _authority(guardian)
    mandate = CopyMandate.parse(verify(envelope, key, "copy-mandate-v1"))
    m = mandate.data
    if guardian._scope(now_ms) or m not in guardian.release.payload["mandates"]:
        raise ValueError("MANDATE_RELEASE_SCOPE")
    state = guardian.journal.snapshot()
    records = state.get("copy_mandates", {})
    existing = records.get(mandate.owner)
    if existing:
        if existing["mandate"] != m:
            raise ValueError("MANDATE_GENERATION_EQUIVOCATION")
        return MandateState.parse(existing["state"])
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
    signed_release = guardian.release.signed_envelope
    assert signed_release is not None
    record = {
        "mandate": m,
        "approval": envelope,
        "authority_public": key.public_bytes_raw().hex(),
        "release_envelope": load_json(signed_release),
        "release_sha": guardian.release.release_sha,
        "state": transition.successor.data,
        "history": [transition.data],
        "control_envelopes": {},
    }
    guardian.journal.update(
        "COPY_MANDATE_ACTIVATE",
        now_ms,
        lambda s: s.setdefault("copy_mandates", {}).update({mandate.owner: record}),
    )
    return transition.successor


def control_mandate(
    guardian: Guardian,
    envelope: dict[str, Any],
    *,
    now_ms: int,
) -> MandateState:
    key = _authority(guardian)
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
    state = guardian.journal.snapshot()
    command_sha = digest(command)
    replay = state.get("mandate_control_nonces", {}).get(command["nonce"])
    if replay:
        if replay["command_sha"] != command_sha:
            raise ValueError("MANDATE_CONTROL_NONCE_CONFLICT")
        return MandateState.parse(replay["result"])
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

    def commit(s: dict[str, Any]) -> None:
        row = s["copy_mandates"][mandate.owner]
        row["state"] = transition.successor.data
        row["history"].append(transition.data)
        row["control_envelopes"][command_sha] = envelope
        s.setdefault("mandate_control_nonces", {})[command["nonce"]] = {
            "command_sha": command_sha,
            "result": transition.successor.data,
        }
        s["runtime"]["permit"] = None

    guardian.journal.update("COPY_MANDATE_CONTROL", now_ms, commit)
    return transition.successor


def baseline_mandate(
    guardian: Guardian,
    owner: str,
    receiver: SourcePacketReceiver,
    *,
    now_ms: int,
) -> MandateState:
    _source(guardian, receiver)
    entry = guardian.journal.snapshot()["copy_mandates"][owner]
    mandate, before = CopyMandate.parse(entry["mandate"]), MandateState.parse(entry["state"])
    if before.phase != "AWAITING_BASELINE":
        return before
    m = mandate.data
    candidates = [
        record
        for record in receiver.checkpoint_outcomes(now_ms=now_ms)
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
        return before
    proof = min(candidates, key=lambda item: item["outcome"]["checkpoint_ms"])
    transition = transition_mandate(
        mandate,
        before,
        event=MandateEvent.COMPLETE_BASELINE,
        evidence_sha=proof["outcome"]["outcome_id"],
        at_ms=now_ms,
        checkpoint_ms=proof["outcome"]["checkpoint_ms"],
    )

    def commit(s: dict[str, Any]) -> None:
        row = s["copy_mandates"][owner]
        row["state"] = transition.successor.data
        row["history"].append(transition.data)
        row["baseline_proof"] = proof

    guardian.journal.update("COPY_MANDATE_BASELINE", now_ms, commit)
    return transition.successor


def validate_mandate_history(previous: dict[str, Any], current: dict[str, Any], kind: str) -> None:
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
            ):
                if old[owner][field] != entry[field]:
                    raise ValueError("MANDATE_IMMUTABLE_GENERATION_REWRITTEN")
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
        ):
            raise ValueError("MANDATE_RELEASE_BINDING")
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
    if kind.startswith("COPY_MANDATE") and any(
        previous.get(field) != current.get(field)
        for field in (
            "finance",
            "positions",
            "pod_positions",
            "fills",
            "intents",
            "reservations",
            "halts",
        )
    ):
        raise ValueError("MANDATE_TRANSITION_MUTATED_FINANCIAL_OBLIGATIONS")
