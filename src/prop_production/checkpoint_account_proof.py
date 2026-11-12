"""One retained account-proof contract for admission and protective replay."""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .evidence import canonical, decimal
from .gateway import BrokerSnapshot, execution_capability, snapshot_from_wire
from .planner import validate_quotes


def _decimal_wire(value: Any) -> str:
    if not isinstance(value, Decimal):
        raise TypeError("ACCOUNT_PROOF_NONCANONICAL_VALUE")
    return str(value)


def capture(snapshot: BrokerSnapshot) -> dict[str, Any]:
    original = json.loads(json.dumps(asdict(snapshot), default=_decimal_wire))
    proof = {
        "account_binding": snapshot.account_binding,
        "endpoint": snapshot.endpoint,
        "observed_at_ms": snapshot.observed_at_ms,
        "received_at_ms": snapshot.received_at_ms,
        "epoch": snapshot.epoch,
        "cash": str(snapshot.cash),
        "equity": str(snapshot.equity),
        "positions": {p.market: str(p.quantity) for p in snapshot.positions if p.quantity},
        "cutoff": snapshot.cutoff,
        "market_marks": [[m, str(p), t] for m, p, t in snapshot.market_marks],
        "execution_capabilities": {
            row["instrument"]: row for row in snapshot.execution_capabilities
        },
        "position_basis": {
            p.market: {"quantity": str(p.quantity), "entry_price": str(p.entry_price)}
            for p in snapshot.positions
            if p.quantity
        },
        "snapshot": original,
    }
    canonical(proof)
    return proof


def validate(
    state: dict[str, Any],
    proof: dict[str, Any],
    quotes: dict[str, Any],
    cfg: dict[str, Any],
    needed: set[str],
    *,
    now_ms: int,
    synthetic: bool,
    feature: str = "net_order",
) -> None:
    """Replay the original normalized broker facts, not a success/readiness flag."""
    try:
        snapshot = snapshot_from_wire(proof["snapshot"])
        if proof != capture(snapshot):
            raise ValueError("PROJECTION")
        if (
            state["runtime"]["certainty"] != "COHERENT"
            or not state["finance"]
            or not snapshot.ready(now_ms, cfg["account_max_age_ms"])
            or snapshot.account_binding != state["account_binding"]
            or snapshot.cutoff != state["runtime"].get("provider_cutoff")
            or not 0 <= now_ms - state["runtime"]["last_snapshot_ms"] <= cfg["account_max_age_ms"]
            or decimal(state["finance"]["cash"]) != snapshot.cash
            or decimal(state["finance"]["equity"]) != snapshot.equity
        ):
            raise ValueError("FINANCE_OR_CUTOFF")
        observed = {p.market: p.quantity for p in snapshot.positions if p.quantity}
        expected = {m: decimal(q) for m, q in state["positions"].items() if decimal(q)}
        if observed != expected or len({p.market for p in snapshot.positions}) != len(
            snapshot.positions
        ):
            raise ValueError("POSITIONS")
        validate_quotes(quotes, now_ms)
        marks = {m: (price, known) for m, price, known in snapshot.market_marks}
        if len(marks) != len(snapshot.market_marks):
            raise ValueError("MARK_EQUIVOCATION")
        caps = proof["execution_capabilities"]
        if len(caps) != len(snapshot.execution_capabilities):
            raise ValueError("CAPABILITY_EQUIVOCATION")
        for market in needed:
            q = quotes[market]
            price, known = marks[market]
            if (
                not 0 <= now_ms - q["known_at_ms"] <= cfg["quote_max_age_ms"]
                or not 0 <= now_ms - known <= cfg["account_max_age_ms"]
                or not decimal(q["bid"]) <= price <= decimal(q["ask"])
                or (decimal(q["ask"]) - decimal(q["bid"])) / decimal(q["bid"]) > Decimal("0.01")
            ):
                raise ValueError("MARK_OR_QUOTE")
            if not execution_capability(
                caps[market],
                feature,
                account=state["account_binding"],
                endpoint=snapshot.endpoint,
                instrument=market,
                now_ms=now_ms,
                synthetic=synthetic,
            ):
                raise ValueError("CAPABILITY")
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("CHECKPOINT_ACCOUNT_PROOF:" + str(exc)) from exc
