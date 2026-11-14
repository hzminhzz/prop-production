"""Explicit signed reductions owned by the existing Guardian, independent of source time."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .checkpoint_account_proof import validate as validate_account_proof
from .checkpoint_accounting import add, split
from .checkpoint_risk import (
    account_anchor,
    check_prerequisites,
    effective_policy,
    execution_policy,
)
from .checkpoint_source import fields, integer
from .contracts import (
    CHECKPOINT_POLICY_V3,
    PORTFOLIO_RELEASE_V2,
    SYNTHETIC_ENDPOINT,
    TERMINAL_INTENT_STATUSES,
    is_synthetic_account,
    is_synthetic_scope,
    require_synthetic_execution,
)
from .copy_mandates import mandate_authority
from .evidence import decimal, digest, load_json, require_sha, verify
from .gateway import BrokerGateway, execution_capability
from .risk import validate_market_inputs

TERMINAL = TERMINAL_INTENT_STATUSES


@dataclass(frozen=True)
class ProtectionState:
    request_id: str
    phase: Literal["UNCOMMITTED", "COMMITTED"]
    intent_id: str | None


@dataclass(frozen=True)
class ProtectionTransition:
    predecessor: ProtectionState
    event: Literal["AUTHORIZE_REDUCTION"]
    evidence_sha: str
    at_ms: int
    actor: Literal["GUARDIAN"]
    guard: Literal["SIGNED_REQUEST_AND_VERIFIED_NATIVE_REDUCTION"]
    successor: ProtectionState
    effect: Literal["RESERVE_ONE_REDUCE_ONLY_INTENT"]

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self)


def command_body(envelope: dict[str, Any], key: Ed25519PublicKey) -> dict[str, Any]:
    body = verify(envelope, key, "guardian-protection-v1")
    fields(
        body,
        {
            "schema",
            "account_binding",
            "release_sha",
            "predecessor_sha",
            "broker_epoch",
            "market",
            "quantity",
            "issued_ms",
            "expires_ms",
            "reason",
            "nonce",
        },
    )
    if body["schema"] != "guardian_protective_reduction_v1" or body["reason"] not in {
        "EXPLICIT_FLATTEN",
        "LOSS_LIMIT",
        "INCIDENT",
    }:
        raise ValueError("PROTECTIVE_REQUEST_SCHEMA")
    for name in ("release_sha", "predecessor_sha", "nonce"):
        require_sha(body[name])
    for name in ("broker_epoch", "issued_ms", "expires_ms"):
        integer(body[name])
    decimal(body["quantity"])
    return body


def derive(state: dict[str, Any], record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    body, proof, at_ms = record["body"], record["account_proof"], record["at_ms"]
    policy, markets, quotes = record["policy"], record["markets"], record["quotes"]
    cfg = effective_policy(state, policy, markets)["checkpoint_execution"]
    market = body["market"]
    validate_market_inputs(markets, policy, {market}, at_ms)
    validate_account_proof(
        state, proof, quotes, cfg, {market}, now_ms=at_ms, synthetic=True, feature="reduce_only"
    )
    if not is_synthetic_scope(state["account_binding"], proof["endpoint"]):
        raise ValueError("PROTECTIVE_SYNTHETIC_SCOPE")
    old, quantity = decimal(state["positions"].get(market, "0")), decimal(body["quantity"])
    if not old or old * quantity >= 0 or abs(quantity) > abs(old):
        raise ValueError("PROTECTIVE_REDUCTION_BOUND")
    if state["reservations"] or any(i["status"] not in TERMINAL for i in state["intents"].values()):
        raise ValueError("PROTECTIVE_UNRESOLVED_OBLIGATIONS")
    if (
        state["runtime"]["certainty"] != "COHERENT"
        or state["account_binding"] != body["account_binding"]
        or body["release_sha"] != state["active_release_sha"]
        or body["broker_epoch"] != state["runtime"]["broker_epoch"]
        or not body["issued_ms"] <= at_ms < body["expires_ms"]
        or record["account_sha"] != account_anchor(state)
        or proof["account_binding"] != state["account_binding"]
        or proof["cash"] != state["finance"]["cash"]
        or proof["equity"] != state["finance"]["equity"]
        or not 0 <= proof["observed_at_ms"] <= proof["received_at_ms"] <= at_ms
        or at_ms - proof["observed_at_ms"] > cfg["account_max_age_ms"]
    ):
        raise ValueError("PROTECTIVE_ACCOUNT_PROOF")
    basis = state.get("checkpoint_basis", {}).get(market)
    if basis is None or proof["position_basis"].get(market) != basis:
        raise ValueError("PROTECTIVE_ENTRY_BASIS_UNQUALIFIED")
    if not execution_capability(
        proof["execution_capabilities"][market],
        "reduce_only",
        account=state["account_binding"],
        endpoint=proof["endpoint"],
        instrument=market,
        now_ms=at_ms,
        synthetic=True,
    ):
        raise ValueError("PROTECTIVE_NATIVE_REDUCE_ONLY_UNQUALIFIED")
    quote, spec = quotes[market], markets[market]
    if not 0 <= at_ms - quote["known_at_ms"] <= cfg["quote_max_age_ms"]:
        raise ValueError("PROTECTIVE_QUOTE_STALE")
    executable = decimal(quote["ask"] if quantity > 0 else quote["bid"])
    if (
        quantity % decimal(spec["qty_step"])
        or abs(quantity) < decimal(spec["min_qty"])
        or abs(quantity * executable) < decimal(spec["min_notional"])
    ):
        raise ValueError("PROTECTIVE_NATIVE_LOT")
    slip = decimal(policy["shortfall_bps"]) / 10000
    price = executable * (1 + slip if quantity > 0 else 1 - slip)
    tick = decimal(spec["price_tick"])
    price = (price / tick).to_integral_value(
        rounding=ROUND_FLOOR if quantity > 0 else ROUND_CEILING
    ) * tick
    owners = {
        owner: abs(decimal(book.get(market, "0")))
        for owner, book in state["pod_positions"].items()
        if decimal(book.get(market, "0")) * old > 0
    }
    if add(*owners.values()) < abs(quantity):
        raise ValueError("PROTECTIVE_POD_OWNERSHIP")
    changes = split(quantity, owners)
    deadline = min(
        body["expires_ms"],
        quote["known_at_ms"] + cfg["quote_max_age_ms"],
        state["runtime"]["last_snapshot_ms"] + cfg["account_max_age_ms"],
    )
    if at_ms >= deadline:
        raise ValueError("PROTECTIVE_ORIGINAL_DEADLINE")
    request_id = digest(["guardian_protection_v1", body["account_binding"], body["nonce"]])
    identity = digest([request_id, market, str(quantity)])
    intent = {
        "intent_id": identity,
        "account_binding": body["account_binding"],
        "endpoint": SYNTHETIC_ENDPOINT,
        "decision_id": request_id,
        "release_sha": body["release_sha"],
        "market": market,
        "quantity": str(quantity),
        "limit_price": str(price),
        "reduce_only": True,
        "status": "QUEUED",
        "filled": "0",
        "broker_id": None,
        "broker_epoch": state["runtime"]["broker_epoch"],
        "boot_epoch": state["runtime"]["boot_epoch"],
        "expires_at_ms": deadline,
        "created_at_ms": at_ms,
        "pod_changes": {p: str(q) for p, q in changes.items()},
        "proof_sha": digest(record),
        "reservation_id": request_id,
        "checkpoint_policy": CHECKPOINT_POLICY_V3,
        "opportunity_ids": [],
        "account_max_age_ms": cfg["account_max_age_ms"],
        "protection_id": request_id,
    }
    cost = abs(quantity * price) * (decimal(spec["fee_rate"]) + slip)
    return intent, {"loss": str(cost), "margin": "0", "cost": str(cost), "intent_ids": [identity]}


def prepare_protective_reduction(
    state: dict[str, Any],
    *,
    head_sha: str,
    release_payload: dict[str, Any],
    release_authority: str,
    signed_release: bytes | None,
    mandate_key: Ed25519PublicKey | None,
    rules_status: str,
    policy: dict[str, Any],
    markets: dict[str, Any],
    gateway: BrokerGateway,
    scope_reasons: list[str],
    envelope: dict[str, Any],
    quotes: dict[str, Any],
    now_ms: int,
) -> tuple[str, dict[str, Any] | None]:
    key = mandate_authority(mandate_key, release_payload, signed=signed_release is not None)
    body = command_body(envelope, key)
    identity = digest(["guardian_protection_v1", body["account_binding"], body["nonce"]])
    prior = state.get("protective_reductions", {}).get(identity)
    if prior is not None:
        if prior["record"]["envelope"] != envelope:
            raise ValueError("PROTECTIVE_NONCE_REUSE")
        return str(prior["transition"]["successor"]["intent_id"]), None
    require_synthetic_execution(
        gateway_synthetic=gateway.synthetic,
        gateway_endpoint=getattr(gateway, "endpoint", None),
        source_mode=release_payload["source_mode"],
        rules_status=rules_status,
        account_binding=state["account_binding"],
    )
    if scope_reasons or body["predecessor_sha"] != head_sha:
        raise ValueError("PROTECTIVE_RELEASE_OR_PREDECESSOR")
    if state["reservations"] or any(i["status"] not in TERMINAL for i in state["intents"].values()):
        raise ValueError("PROTECTIVE_UNRESOLVED_OBLIGATIONS")
    cfg = execution_policy(state, policy, markets)
    reasons, proof = check_prerequisites(
        gateway,
        state,
        quotes,
        cfg,
        now_ms,
        {body["market"]},
        capability_feature="reduce_only",
    )
    if reasons:
        raise ValueError("PROTECTIVE_PREREQUISITES:" + ",".join(reasons))
    assert signed_release is not None
    next_state = copy.deepcopy(state)
    record = {
        "body": body,
        "envelope": envelope,
        "at_ms": now_ms,
        "authority_public": key.public_bytes_raw().hex(),
        "release_envelope": load_json(signed_release),
        "release_authority": release_authority,
        "account_proof": proof,
        "account_sha": account_anchor(next_state),
        "policy": policy,
        "markets": markets,
        "quotes": quotes,
    }
    intent, reservation = derive(next_state, record)
    transition = ProtectionTransition(
        ProtectionState(identity, "UNCOMMITTED", None),
        "AUTHORIZE_REDUCTION",
        digest(record),
        now_ms,
        "GUARDIAN",
        "SIGNED_REQUEST_AND_VERIFIED_NATIVE_REDUCTION",
        ProtectionState(identity, "COMMITTED", intent["intent_id"]),
        "RESERVE_ONE_REDUCE_ONLY_INTENT",
    )
    next_state.setdefault("protective_reductions", {})[identity] = {
        "record": record,
        "transition": transition.data,
    }
    next_state["intents"][intent["intent_id"]] = intent
    next_state["reservations"][identity] = reservation
    next_state["runtime"].update(
        state="HALTED" if next_state["halts"] else "SHADOW_READY",
        flat_confirmed=False,
        permit={
            "decision_id": identity,
            "boot_epoch": next_state["runtime"]["boot_epoch"],
            "broker_epoch": next_state["runtime"]["broker_epoch"],
            "expires_at_ms": intent["expires_at_ms"],
            "reduce_only": True,
        },
    )
    return str(intent["intent_id"]), next_state


def valid_protective_permit(state: dict[str, Any]) -> bool:
    permit = state["runtime"].get("permit")
    if not isinstance(permit, dict) or permit.get("reduce_only") is not True:
        return False
    row = state.get("protective_reductions", {}).get(permit.get("decision_id"))
    if row is None:
        return False
    identity = row["transition"]["successor"]["intent_id"]
    intent = state["intents"].get(identity)
    if intent is None or intent.get("protection_id") != permit.get("decision_id"):
        return False
    return bool(
        intent["reduce_only"] is True
        and is_synthetic_account(state["account_binding"])
        and intent["boot_epoch"] == state["runtime"]["boot_epoch"] == permit.get("boot_epoch")
        and intent["broker_epoch"] == state["runtime"]["broker_epoch"] == permit.get("broker_epoch")
        and intent["expires_at_ms"] == permit.get("expires_at_ms")
        and set(permit)
        == {"decision_id", "boot_epoch", "broker_epoch", "expires_at_ms", "reduce_only"}
    )


def validate_protection(
    previous: dict[str, Any], current: dict[str, Any], kind: str, previous_head: str | None
) -> None:
    old, new = previous.get("protective_reductions", {}), current.get("protective_reductions", {})
    if not set(old) <= set(new) or any(new[k] != row for k, row in old.items()):
        raise ValueError("PROTECTIVE_HISTORY_REWRITE")
    for identity in set(new) - set(old):
        if kind != "PROTECTIVE_RESERVE":
            raise ValueError("PROTECTIVE_COMMIT_KIND")
        row = new[identity]
        record = row["record"]
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(record["authority_public"]))
        body = command_body(record["envelope"], key)
        release = verify(record["release_envelope"], key, record["release_authority"])
        if (
            body != record["body"]
            or body["predecessor_sha"] != previous_head
            or digest(release) != previous["active_release_sha"]
            or digest(release) != body["release_sha"]
            or release["schema"] != PORTFOLIO_RELEASE_V2
            or release["source_mode"] != "SYNTHETIC"
            or release["mandate_authority_sha"] != sha256(key.public_bytes_raw()).hexdigest()
            or digest({"policy": record["policy"], "markets": record["markets"]})
            != release["risk_policy_sha"]
        ):
            raise ValueError("PROTECTIVE_SIGNED_AUTHORITY")
        intent, reservation = derive(previous, record)
        expected = ProtectionTransition(
            ProtectionState(identity, "UNCOMMITTED", None),
            "AUTHORIZE_REDUCTION",
            digest(record),
            record["at_ms"],
            "GUARDIAN",
            "SIGNED_REQUEST_AND_VERIFIED_NATIVE_REDUCTION",
            ProtectionState(identity, "COMMITTED", intent["intent_id"]),
            "RESERVE_ONE_REDUCE_ONLY_INTENT",
        )
        if (
            identity != intent["protection_id"]
            or row["transition"] != expected.data
            or current["intents"].get(intent["intent_id"]) != intent
            or current["reservations"].get(identity) != reservation
            or any(
                current[k] != previous[k]
                for k in ("positions", "pod_positions", "finance", "fills", "halts")
            )
        ):
            raise ValueError("PROTECTIVE_INTENT_DERIVATION")
    for intent in current.get("intents", {}).values():
        if intent.get("protection_id") is not None:
            row = new.get(intent["protection_id"])
            if row is None or row["transition"]["successor"]["intent_id"] != intent["intent_id"]:
                raise ValueError("PROTECTIVE_INTENT_UNBOUND")
            if intent["reduce_only"] is not True:
                raise ValueError("PROTECTIVE_INTENT_MUST_REDUCE")
