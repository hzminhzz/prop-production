"""Immutable canonical records at the Planner -> Guardian seam."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import canonical, digest, load_json, require_sha

TERMINAL_BROKER_STATUSES = frozenset(
    {"FILLED", "CANCELLED", "EXPIRED", "REJECTED", "NOT_ACCEPTED"}
)
TERMINAL_INTENT_STATUSES = TERMINAL_BROKER_STATUSES | frozenset({"REVOKED"})
ZERO_FILL_TERMINAL_STATUSES = frozenset({"REJECTED", "NOT_ACCEPTED", "REVOKED"})
ACTIVE_BROKER_ORDER_STATUSES = frozenset(
    {"QUEUED", "ACCEPTED", "PARTIAL", "UNKNOWN", "DISPATCHING"}
)
ALLOWED_BROKER_ORDER_STATUSES = TERMINAL_BROKER_STATUSES | ACTIVE_BROKER_ORDER_STATUSES
ALLOWED_INTENT_STATUSES = ALLOWED_BROKER_ORDER_STATUSES | frozenset(
    {"ACKNOWLEDGED", "REJECT_PENDING_PROOF", "CANCEL_PENDING", "REVOKED"}
)

PORTFOLIO_RELEASE_V1 = "portfolio_release_v1"
PORTFOLIO_RELEASE_V2 = "portfolio_release_v2"
PORTFOLIO_RELEASE_SCHEMAS = frozenset({PORTFOLIO_RELEASE_V1, PORTFOLIO_RELEASE_V2})
CHECKPOINT_SCHEMA_V3 = "cwsa-checkpoint-v3"
CHECKPOINT_POLICY_V3 = "CWSA_CHECKPOINT_V3"
V4_EXPORT_V1 = "v4_signed_source_export_v1"
V4_EXPORT_V2 = "v4_signed_source_export_v2"
V4_BINDING_V1 = "v4_source_batch_binding_v1"
V4_BINDING_V2 = "v4_source_batch_binding_v2"

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


@dataclass(frozen=True)
class TargetBatchProposal:
    payload: bytes

    @property
    def data(self) -> dict[str, Any]:
        return dict(load_json(self.payload))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TargetBatchProposal:
        required = {"schema", "proposal_id", "release_sha", "account_binding", "first_known_ms",
                    "due_ms", "decision_ms", "broker_epoch", "pods", "source_receipts", "quote_sha"}
        if set(value) != required or value["schema"] != "target_batch_proposal_v1":
            raise ValueError("MALFORMED_PROPOSAL")
        require_sha(value["release_sha"])
        require_sha(value["quote_sha"])
        for field in ("first_known_ms", "due_ms", "decision_ms", "broker_epoch"):
            if type(value[field]) is not int or value[field] < 0:
                raise ValueError("INVALID_PROPOSAL_CLOCK")
        if value["due_ms"] != value["first_known_ms"] + 300000:
            raise ValueError("INVALID_BATCH_WINDOW")
        if value["decision_ms"] < value["due_ms"]:
            raise ValueError("EARLY_DECISION")
        if not value["pods"] or not value["source_receipts"]:
            raise ValueError("EMPTY_PROPOSAL")
        body = {k: v for k, v in value.items() if k != "proposal_id"}
        if value["proposal_id"] != digest(body):
            raise ValueError("PROPOSAL_ID_CONFLICT")
        return cls(canonical(value))


@dataclass(frozen=True)
class DecisionReceipt:
    payload: bytes

    @property
    def data(self) -> dict[str, Any]:
        return dict(load_json(self.payload))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DecisionReceipt:
        return cls(canonical(value))
