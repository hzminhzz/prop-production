"""Version-specific, replayable Guardian journal semantics.

Hash integrity is not sufficient to prove financial legality. V1 history is
preserved as legacy-unverifiable; v2 checks run before commit and during replay.
This module never recreates broker truth or acts as a second financial ledger.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .evidence import decimal

CONTRACT = "guardian_finance_semantic_v2"
_ALLOWED_INTENT_STATUSES = frozenset(
    {
        "QUEUED",
        "DISPATCHING",
        "ACKNOWLEDGED",
        "REJECT_PENDING_PROOF",
        "CANCEL_PENDING",
        "UNKNOWN",
        "ACCEPTED",
        "PARTIAL",
        "FILLED",
        "CANCELLED",
        "EXPIRED",
        "REJECTED",
        "NOT_ACCEPTED",
        "REVOKED",
    }
)
_ALLOWED_RUNTIME = frozenset(
    {
        "DISARMED",
        "ARMED",
        "HALTED",
        "UNRESOLVED",
        "ARMING",
        "VERIFYING",
        "SHADOW_READY",
    }
)


def validate_transition(
    previous: dict[str, Any],
    current: dict[str, Any],
    kind: str,
    *,
    previous_head: str,
) -> None:
    """Reject impossible reservations, fill accounting or history rewrites."""
    from .guardian_checkpoints import validate_mandate_history

    validate_mandate_history(previous, current, kind)
    if current.get("_semantic_contract") != CONTRACT:
        raise ValueError("GUARDIAN_SEMANTIC_CONTRACT_NOT_V2")
    if previous and previous.get("_semantic_contract") != CONTRACT:
        checkpoint = current.get("_semantic_checkpoint")
        if (
            kind != "SEMANTIC_CHECKPOINT_V2"
            or not isinstance(checkpoint, dict)
            or checkpoint.get("legacy_head") != previous_head
            or checkpoint.get("legacy_sequence") != previous.get("sequence")
            or not isinstance(checkpoint.get("independent_remediation_sha"), str)
            or len(checkpoint["independent_remediation_sha"]) != 64
        ):
            raise ValueError("GUARDIAN_LEGACY_UPGRADE_WITHOUT_FORWARD_CHECKPOINT")
    if previous and current["account_binding"] != previous["account_binding"]:
        raise ValueError("GUARDIAN_SEMANTIC_ACCOUNT_CHANGED")
    halts = current.get("halts")
    runtime = current.get("runtime")
    if (
        not isinstance(halts, list)
        or len(set(halts)) != len(halts)
        or not all(isinstance(x, str) and x for x in halts)
        or not isinstance(runtime, dict)
        or runtime.get("state") not in _ALLOWED_RUNTIME
        or type(runtime.get("boot_epoch")) is not int
        or type(runtime.get("broker_epoch")) is not int
        or runtime["boot_epoch"] < 0
        or runtime["broker_epoch"] < 0
        or (halts and runtime.get("permit") is not None)
    ):
        raise ValueError("GUARDIAN_SEMANTIC_HALT_OR_PERMIT_INVALID")
    if not (
        isinstance(current.get("intents"), dict)
        and isinstance(current.get("fills"), dict)
        and isinstance(current.get("positions"), dict)
        and isinstance(current.get("reservations"), dict)
    ):
        raise ValueError("GUARDIAN_SEMANTIC_FINANCIAL_SHAPE")
    intents: dict[str, Any] = current["intents"]
    fills: dict[str, Any] = current["fills"]
    positions: dict[str, Any] = current["positions"]
    reservations: dict[str, Any] = current["reservations"]
    by_intent: dict[str, Decimal] = defaultdict(Decimal)
    by_market: dict[str, Decimal] = defaultdict(Decimal)
    for execution_id, fill in fills.items():
        if (
            not isinstance(fill, dict)
            or execution_id != fill.get("execution_id")
            or fill.get("intent_id") not in intents
            or fill.get("market") != intents[fill["intent_id"]].get("market")
        ):
            raise ValueError("GUARDIAN_SEMANTIC_ORPHAN_OR_REBOUND_FILL")
        quantity = decimal(fill["quantity"])
        if quantity == 0 or decimal(fill["price"]) <= 0 or decimal(fill["fee"]) < 0:
            raise ValueError("GUARDIAN_SEMANTIC_FILL_ECONOMICS_INVALID")
        by_intent[fill["intent_id"]] += quantity
        by_market[fill["market"]] += quantity
    for intent_id, intent in intents.items():
        if (
            not isinstance(intent, dict)
            or intent.get("intent_id") != intent_id
            or intent.get("account_binding") != current["account_binding"]
            or intent.get("status") not in _ALLOWED_INTENT_STATUSES
            or not isinstance(intent.get("market"), str)
            or not intent["market"]
        ):
            raise ValueError("GUARDIAN_SEMANTIC_INTENT_IDENTITY_INVALID")
        quantity = decimal(intent["quantity"])
        filled = decimal(intent["filled"])
        if (
            quantity == 0
            or filled * quantity < 0
            or abs(filled) > abs(quantity)
            or filled != by_intent[intent_id]
            or (intent["status"] == "FILLED" and filled != quantity)
            or (intent["status"] in {"REJECTED", "NOT_ACCEPTED"} and filled != 0)
        ):
            raise ValueError("GUARDIAN_SEMANTIC_INTENT_FILL_CONSERVATION")
    recorded_positions = {market: decimal(qty) for market, qty in positions.items()}
    for market in set(by_market) | set(recorded_positions):
        if by_market[market] != recorded_positions.get(market, Decimal(0)):
            raise ValueError("GUARDIAN_SEMANTIC_POSITION_FILL_CONSERVATION")
    for reservation_id, reservation in reservations.items():
        if (
            not isinstance(reservation, dict)
            or not isinstance(reservation.get("intent_ids"), list)
            or not reservation["intent_ids"]
            or any(identity not in intents for identity in reservation["intent_ids"])
            or len(set(reservation["intent_ids"])) != len(reservation["intent_ids"])
        ):
            raise ValueError("GUARDIAN_SEMANTIC_RESERVATION_IDENTITY_INVALID")
    if previous and previous.get("_semantic_contract") == CONTRACT:
        removed_halts = set(previous["halts"]) - set(halts)
        if removed_halts or kind.startswith(("RECOVER_AUTH_", "RECOVER_")):
            allowed = False
            if len(removed_halts) == 1 and not (set(halts) - set(previous["halts"])):
                reason = next(iter(removed_halts))
                expected_state = "HALTED" if halts else "DISARMED"
                basic = (
                    runtime["state"] == expected_state
                    and runtime.get("permit") is None
                    and runtime["boot_epoch"] == previous["runtime"]["boot_epoch"] + 1
                    and isinstance(runtime.get("last_recovery"), dict)
                    and runtime["last_recovery"].get("reason") == reason
                    and runtime["last_recovery"].get("remaining_halts") == halts
                )
                if kind == "RECOVER_" + reason and previous["account_binding"] == "fixture-account":
                    allowed = basic
                elif kind == "RECOVER_AUTH_" + reason:
                    recovery = runtime["last_recovery"]
                    before_nonces = previous.get("recovery_nonces", {})
                    after_nonces = current.get("recovery_nonces", {})
                    new_nonces = (
                        set(after_nonces) - set(before_nonces)
                        if isinstance(after_nonces, dict) and isinstance(before_nonces, dict)
                        else set()
                    )
                    allowed = (
                        basic
                        and runtime["broker_epoch"] == previous["runtime"]["broker_epoch"] + 1
                        and recovery.get("recovery_state") == "RECOVERY_VERIFIED"
                        and recovery.get("account_binding") == current["account_binding"]
                        and len(new_nonces) == 1
                        and all(
                            isinstance(recovery.get(field), str) and len(recovery[field]) == 64
                            for field in ("request_sha", "remediation_sha", "recovery_key_sha")
                        )
                        and any(
                            after_nonces[nonce].get("request_sha") == recovery["request_sha"]
                            and after_nonces[nonce].get("remediation_sha")
                            == recovery["remediation_sha"]
                            and after_nonces[nonce].get("head_at_request") == previous_head
                            for nonce in new_nonces
                            if isinstance(after_nonces[nonce], dict)
                        )
                    )
            if not allowed:
                raise ValueError("GUARDIAN_SEMANTIC_UNAUTHORIZED_HALT_CLEARANCE")
        if not set(previous["fills"]).issubset(fills):
            raise ValueError("GUARDIAN_SEMANTIC_FILL_HISTORY_REMOVED")
        if any(previous["fills"][key] != fills[key] for key in previous["fills"]):
            raise ValueError("GUARDIAN_SEMANTIC_FILL_REWRITTEN")
        if not set(previous["intents"]).issubset(intents):
            raise ValueError("GUARDIAN_SEMANTIC_INTENT_HISTORY_REMOVED")
        if kind.startswith("HALT_") and any(
            previous[field] != current[field]
            for field in (
                "finance",
                "positions",
                "pod_positions",
                "fills",
                "intents",
                "reservations",
            )
        ):
            raise ValueError("GUARDIAN_SEMANTIC_HALT_MUTATED_FINANCE")
        if kind.startswith("RECOVER_AUTH_") and any(
            previous[field] != current[field]
            for field in (
                "finance",
                "positions",
                "pod_positions",
                "fills",
                "intents",
                "reservations",
            )
        ):
            raise ValueError("GUARDIAN_SEMANTIC_RECOVERY_MUTATED_FINANCE")
