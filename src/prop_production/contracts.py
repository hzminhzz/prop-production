"""Immutable canonical records at the Planner -> Guardian seam."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import canonical, digest, load_json, require_sha


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
