"""Reason-specific protected HALT clearance using independently observed broker truth.

A signed operator intent is not financial remediation. The Guardian journal is
the only effect owner; the pinned BrokerGateway is queried outside its transaction.
Propr/Bybit readers without coherent financial cutoffs cannot pass this gate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .evidence import decimal, digest
from .gateway import BrokerSnapshot

_TERMINAL = frozenset({"FILLED", "CANCELLED", "EXPIRED", "REJECTED", "NOT_ACCEPTED"})
_REMEDIABLE = frozenset(
    {
        "BROKER_UNREACHABLE",
        "UNKNOWN_OUTCOME",
        "RECONCILIATION_UNCERTAIN",
        "BROKER_CLOCK_REGRESSION",
        "BROKER_FALSE_FILLED_TERMINAL",
        "BROKER_TERMINAL_EXECUTION_CONFLICT",
        "BROKER_STATUS_UNKNOWN",
        "BROKER_ORDER_UNKNOWN",
        "ORDER_FILL_MISMATCH",
    }
)


def snapshot_receipt(snapshot: BrokerSnapshot) -> dict[str, Any]:
    """Deterministic no-secret provider cutoff, orders, fills and account proof."""
    return {
        "schema": "guardian_broker_remediation_evidence_v1",
        "account_binding": snapshot.account_binding,
        "endpoint": snapshot.endpoint,
        "observed_at_ms": snapshot.observed_at_ms,
        "epoch": snapshot.epoch,
        "cutoff": snapshot.cutoff,
        "history_complete": snapshot.history_complete,
        "financial_complete": snapshot.financial_complete,
        "connected": snapshot.connected,
        "cash": str(snapshot.cash) if snapshot.cash is not None else None,
        "equity": str(snapshot.equity) if snapshot.equity is not None else None,
        "unrealized": str(snapshot.unrealized) if snapshot.unrealized is not None else None,
        "orders": [
            {
                "intent_id": o.intent_id,
                "broker_id": o.broker_id,
                "market": o.market,
                "quantity": str(o.quantity),
                "filled": str(o.filled),
                "status": o.status,
                "reduce_only": o.reduce_only,
            }
            for o in sorted(snapshot.orders, key=lambda row: row.intent_id)
        ],
        "fills": [
            {
                "execution_id": f.execution_id,
                "intent_id": f.intent_id,
                "market": f.market,
                "quantity": str(f.quantity),
                "price": str(f.price),
                "fee": str(f.fee),
                "known_at_ms": f.known_at_ms,
            }
            for f in sorted(snapshot.fills, key=lambda row: row.execution_id)
        ],
        "positions": [
            {
                "market": p.market,
                "quantity": str(p.quantity),
                "entry_price": str(p.entry_price),
                "mark_price": str(p.mark_price),
            }
            for p in sorted(snapshot.positions, key=lambda row: row.market)
        ],
    }


def require_real_remediation(
    *,
    reason: str,
    state: dict[str, Any],
    snapshot: BrokerSnapshot,
    now_ms: int,
) -> str:
    """External conservation oracle: no Guardian risk calculation is reused."""
    if reason not in _REMEDIABLE:
        raise ValueError("RECOVERY_REASON_HAS_NO_VERIFIED_REMEDIATION_POLICY")
    if snapshot.account_binding != state["account_binding"]:
        raise ValueError("RECOVERY_BROKER_ACCOUNT_MISMATCH")
    if not snapshot.ready(now_ms, 5000):
        raise ValueError("RECOVERY_PROVIDER_HISTORY_OR_FINANCE_UNVERIFIED")
    runtime = state["runtime"]
    if (
        runtime.get("certainty") != "COHERENT"
        or runtime.get("provider_cutoff") != snapshot.cutoff
        or not isinstance(runtime.get("last_snapshot_ms"), int)
        or snapshot.observed_at_ms < runtime["last_snapshot_ms"]
    ):
        raise ValueError("RECOVERY_CURRENT_GUARDIAN_RECONCILIATION_MISSING")
    expected = {
        market: decimal(qty) for market, qty in state["positions"].items() if decimal(qty) != 0
    }
    observed = {p.market: p.quantity for p in snapshot.positions if p.quantity}
    if expected != observed or len(snapshot.positions) != len(observed):
        raise ValueError("RECOVERY_BROKER_POSITION_NOT_CONSERVED")
    if state.get("finance", {}).get("cash") is not None and (
        decimal(state["finance"]["cash"]) != snapshot.cash
    ):
        raise ValueError("RECOVERY_BROKER_CASH_NOT_CONSERVED")
    orders = {o.intent_id: o for o in snapshot.orders}
    if len(orders) != len(snapshot.orders):
        raise ValueError("RECOVERY_PROVIDER_ORDER_EQUIVOCATION")
    fills: dict[str, Decimal] = {}
    execution_ids: set[str] = set()
    for f in snapshot.fills:
        if f.execution_id in execution_ids or f.intent_id not in orders:
            raise ValueError("RECOVERY_DUPLICATE_OR_ORPHAN_EXECUTION")
        execution_ids.add(f.execution_id)
        fills[f.intent_id] = fills.get(f.intent_id, Decimal(0)) + f.quantity
        recorded = state["fills"].get(f.execution_id)
        if (
            recorded is None
            or recorded["intent_id"] != f.intent_id
            or recorded["market"] != f.market
            or decimal(recorded["quantity"]) != f.quantity
            or decimal(recorded["price"]) != f.price
            or decimal(recorded["fee"]) != f.fee
        ):
            raise ValueError("RECOVERY_EXECUTION_NOT_ACCOUNTED")
    for identity, intent in state["intents"].items():
        order = orders.get(identity)
        if order is None:
            raise ValueError("RECOVERY_UNRESOLVED_INTENT_HISTORY")
        actual = fills.get(identity, Decimal(0))
        if (
            order.market != intent["market"]
            or order.quantity != decimal(intent["quantity"])
            or order.filled != actual
            or decimal(intent["filled"]) != actual
            or order.reduce_only is not intent["reduce_only"]
            or order.status not in _TERMINAL
            or (order.status == "FILLED" and actual != order.quantity)
            or (order.status in {"REJECTED", "NOT_ACCEPTED"} and actual != 0)
        ):
            raise ValueError("RECOVERY_TERMINALITY_NOT_CONSERVED")
    if set(orders) - set(state["intents"]):
        raise ValueError("RECOVERY_ORPHAN_BROKER_ORDER")
    if state["reservations"]:
        raise ValueError("RECOVERY_OUTSTANDING_RESERVATIONS")
    return digest(snapshot_receipt(snapshot))
