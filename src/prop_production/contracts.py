"""Immutable canonical records at the Planner -> Guardian seam."""

from __future__ import annotations

from typing import Any

TERMINAL_BROKER_STATUSES = frozenset({"FILLED", "CANCELLED", "EXPIRED", "REJECTED", "NOT_ACCEPTED"})
TERMINAL_INTENT_STATUSES = TERMINAL_BROKER_STATUSES | frozenset({"REVOKED"})
ZERO_FILL_TERMINAL_STATUSES = frozenset({"REJECTED", "NOT_ACCEPTED", "REVOKED"})
ACTIVE_BROKER_ORDER_STATUSES = frozenset(
    {"QUEUED", "ACCEPTED", "PARTIAL", "UNKNOWN", "DISPATCHING"}
)
ALLOWED_BROKER_ORDER_STATUSES = TERMINAL_BROKER_STATUSES | ACTIVE_BROKER_ORDER_STATUSES
ALLOWED_INTENT_STATUSES = ALLOWED_BROKER_ORDER_STATUSES | frozenset(
    {"ACKNOWLEDGED", "REJECT_PENDING_PROOF", "CANCEL_PENDING", "REVOKED"}
)

PORTFOLIO_RELEASE_V2 = "portfolio_release_v2"
PORTFOLIO_RELEASE_SCHEMAS = frozenset({PORTFOLIO_RELEASE_V2})
CHECKPOINT_SCHEMA_V3 = "cwsa-checkpoint-v3"
CHECKPOINT_POLICY_V3 = "CWSA_CHECKPOINT_V3"

SYNTHETIC_ACCOUNT = "fixture-account"
SYNTHETIC_ENDPOINT = "fake://broker"


def is_synthetic_account(account_binding: str) -> bool:
    return account_binding == SYNTHETIC_ACCOUNT


def is_synthetic_scope(account_binding: str, endpoint: str) -> bool:
    return account_binding == SYNTHETIC_ACCOUNT and endpoint == SYNTHETIC_ENDPOINT


def require_synthetic_execution(
    *,
    gateway_synthetic: bool,
    gateway_endpoint: Any = SYNTHETIC_ENDPOINT,
    source_mode: str,
    rules_status: str,
    account_binding: str,
) -> None:
    if (
        gateway_synthetic is not True
        or source_mode != "SYNTHETIC"
        or rules_status != "SYNTHETIC"
        or not isinstance(gateway_endpoint, str)
        or not is_synthetic_scope(account_binding, gateway_endpoint)
    ):
        raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")
