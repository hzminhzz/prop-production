"""Typed average-cost transitions, subsequently checked against independent broker facts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from typing import Any, Literal

from .evidence import decimal, digest


@dataclass(frozen=True)
class BasisState:
    market: str
    quantity: str
    entry_price: str


@dataclass(frozen=True)
class BasisTransition:
    predecessor: BasisState
    event: Literal["BROKER_FILL"]
    evidence_sha: str
    at_ms: int
    actor: Literal["GUARDIAN"]
    guard: Literal["RETAINED_BASIS_AND_ACTUAL_EXECUTION"]
    successor: BasisState
    realized: str

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self)


def advance_basis(
    state: dict[str, Any], fill: dict[str, Any], *, before_quantity: Decimal
) -> BasisTransition:
    market = fill["market"]
    quantity, price = decimal(fill["quantity"]), decimal(fill["price"])
    prior = state.get("checkpoint_basis", {}).get(market)
    if prior is None:
        if before_quantity:
            raise ValueError("CHECKPOINT_OLD_ENTRY_BASIS_UNKNOWN")
        prior = {"quantity": "0", "entry_price": "0"}
    if decimal(prior["quantity"]) != before_quantity:
        raise ValueError("CHECKPOINT_BASIS_QUANTITY")
    entry = decimal(prior["entry_price"])
    if before_quantity and entry <= 0:
        raise ValueError("CHECKPOINT_BASIS_PRICE")
    before = BasisState(market, prior["quantity"], prior["entry_price"])
    # This is the declared synthetic linear average-cost contract. Native
    # rounding/accounting remains unqualified and native dispatch stays denied.
    with localcontext() as context:
        context.prec = 28
        closed = (
            min(abs(before_quantity), abs(quantity))
            if before_quantity * quantity < 0
            else Decimal(0)
        )
        realized = closed * (price - entry) * (1 if before_quantity > 0 else -1)
        after_quantity = before_quantity + quantity
        if before_quantity * quantity >= 0:
            after_entry = (abs(before_quantity) * entry + abs(quantity) * price) / abs(
                after_quantity
            )
        elif after_quantity == 0:
            after_entry = Decimal(0)
        elif before_quantity * after_quantity < 0:
            after_entry = price
        else:
            after_entry = entry
    after = BasisState(market, str(after_quantity), str(after_entry))
    return BasisTransition(
        before,
        "BROKER_FILL",
        digest(fill),
        fill["known_at_ms"],
        "GUARDIAN",
        "RETAINED_BASIS_AND_ACTUAL_EXECUTION",
        after,
        str(realized),
    )
