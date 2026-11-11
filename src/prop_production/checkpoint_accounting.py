"""Exact pod attribution in the existing financial journal; no external executions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal

from .evidence import decimal, digest

ZERO = Decimal(0)
POLICY = "CWSA_CHECKPOINT_V3"


def finite(value: Fraction) -> Decimal:
    """Convert a terminating rational without ambient Decimal-context rounding."""
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("NONTERMINATING_ATTRIBUTION_AMOUNT")
    places = max(twos, fives)
    coefficient = value.numerator * 2 ** (places - twos) * 5 ** (places - fives)
    return Decimal((int(coefficient < 0), tuple(map(int, str(abs(coefficient)))), -places))


def add(*values: Decimal) -> Decimal:
    return finite(sum((Fraction(v) for v in values), Fraction(0)))


def split(total: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """Deterministic finite shares; the last canonical owner retains the exact remainder.

    Attribution precision is at least 18 decimal places and preserves every input
    decimal place. This is bookkeeping precision, never a native trading lot rule.
    """
    keys = sorted(k for k, v in weights.items() if v)
    denominator = sum((Fraction(weights[k]) for k in keys), Fraction(0))
    if not keys or not denominator:
        raise ValueError("ATTRIBUTION_WEIGHTS")
    exponent = min(
        -18, int(total.as_tuple().exponent), *(int(weights[k].as_tuple().exponent) for k in keys)
    )
    quantum = Fraction(10) ** exponent
    units = Fraction(total) / quantum
    if units.denominator != 1:
        raise ValueError("ATTRIBUTION_QUANTUM")
    remaining, allocated = units.numerator, {}
    for key in keys[:-1]:
        share = int(units * Fraction(weights[key]) / denominator)
        allocated[key] = finite(share * quantum)
        remaining -= share
    allocated[keys[-1]] = finite(remaining * quantum)
    return {k: allocated.get(k, ZERO) for k in sorted(weights)}


@dataclass(frozen=True)
class AttributionTransition:
    predecessor: str
    event: Literal["BROKER_FILL", "INTERNAL_NET"]
    evidence_sha: str
    at_ms: int
    actor: Literal["GUARDIAN"]
    guard: Literal["ACTUAL_EXECUTION_CONSERVES", "ZERO_EXTERNAL_CHANGE_NO_EXECUTION"]
    successor: str
    effects: dict[str, Any]

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self)


def _anchor(state: dict[str, Any]) -> str:
    return digest(
        {"pod_positions": state["pod_positions"], "pod_fees_paid": state.get("pod_fees_paid", {})}
    )


def apply_fill(
    state: dict[str, Any],
    fill: dict[str, Any],
    *,
    previous_filled: Decimal,
    before_quantity: Decimal,
) -> dict[str, Any]:
    intent = state["intents"][fill["intent_id"]]
    changes = {p: decimal(q) for p, q in intent["pod_changes"].items()}
    quantity, price, fee = (decimal(fill[k]) for k in ("quantity", "price", "fee"))
    total = add(previous_filled, quantity)
    if add(*changes.values()) != decimal(intent["quantity"]):
        raise ValueError("ATTRIBUTION_INTENT_CONSERVATION")
    before = split(previous_filled, changes)
    after = split(total, changes) if total != decimal(intent["quantity"]) else changes
    fees = split(fee, {p: abs(q) for p, q in changes.items()})
    from .checkpoint_basis import advance_basis

    basis = advance_basis(state, fill, before_quantity=before_quantity)
    realized = decimal(basis.realized)
    closing_weights = {
        owner: min(
            abs(decimal(state["pod_positions"].get(owner, {}).get(fill["market"], "0"))),
            abs(add(after[owner], -before[owner])),
        )
        for owner in changes
        if decimal(state["pod_positions"].get(owner, {}).get(fill["market"], "0"))
        * add(after[owner], -before[owner])
        < 0
    }
    if realized and not any(closing_weights.values()):
        raise ValueError("CHECKPOINT_REALIZED_OWNER_UNPROVEN")
    realized_shares = split(realized, closing_weights) if realized else {}
    predecessor, effects = _anchor(state), {}
    for owner in sorted(changes):
        allocated = add(after[owner], -before[owner])
        notional = finite(Fraction(allocated) * Fraction(price))
        book = state["pod_positions"].setdefault(owner, {})
        book[fill["market"]] = str(add(decimal(book.get(fill["market"], "0")), allocated))
        paid = state.setdefault("pod_fees_paid", {})
        paid[owner] = str(add(decimal(paid.get(owner, "0")), fees[owner]))
        effects[owner] = {
            "quantity": str(allocated),
            "notional": str(notional),
            "fee": str(fees[owner]),
            "realized": str(realized_shares.get(owner, ZERO)),
            "cash_delta": str(add(realized_shares.get(owner, ZERO), -fees[owner])),
        }
    transition = AttributionTransition(
        predecessor,
        "BROKER_FILL",
        digest(fill),
        fill["known_at_ms"],
        "GUARDIAN",
        "ACTUAL_EXECUTION_CONSERVES",
        _anchor(state),
        effects,
    )
    for owner, amount in realized_shares.items():
        earned = state.setdefault("pod_realized", {})
        earned[owner] = str(add(decimal(earned.get(owner, "0")), amount))
    state.setdefault("checkpoint_basis", {})[fill["market"]] = {
        "quantity": basis.successor.quantity,
        "entry_price": basis.successor.entry_price,
    }
    state.setdefault("checkpoint_allocations", {})[fill["execution_id"]] = transition.data | {
        "basis": basis.data
    }
    return effects


def apply_internal(
    state: dict[str, Any], record: dict[str, Any], decision_id: str, now_ms: int
) -> None:
    attempt = record["attempts"][-1]
    risk = attempt["execution_plan"]
    for market in sorted(risk["targets"]):
        if decimal(risk["targets"][market]) != decimal(state["positions"].get(market, "0")):
            continue
        changes = {
            p: add(
                decimal(book.get(market, "0")),
                -decimal(state["pod_positions"].get(p, {}).get(market, "0")),
            )
            for p, book in risk["pod_targets"].items()
        }
        if not any(changes.values()):
            continue
        if add(*changes.values()) != ZERO:
            raise ValueError("INTERNAL_NET_CONSERVATION")
        predecessor = _anchor(state)
        for owner in sorted(changes):
            book = state["pod_positions"].setdefault(owner, {})
            book[market] = str(add(decimal(book.get(market, "0")), changes[owner]))
        identity = digest([decision_id, market])
        transition = AttributionTransition(
            predecessor,
            "INTERNAL_NET",
            digest(attempt),
            now_ms,
            "GUARDIAN",
            "ZERO_EXTERNAL_CHANGE_NO_EXECUTION",
            _anchor(state),
            {
                "market": market,
                "decision_id": decision_id,
                "quantities": {p: str(q) for p, q in sorted(changes.items())},
                "fee": "0",
                "cash_delta": "0",
                "realized": "0",
            },
        )
        state.setdefault("checkpoint_internal_effects", {})[identity] = transition.data


def validate_attribution(previous: dict[str, Any], current: dict[str, Any], kind: str) -> None:
    for key in ("checkpoint_allocations", "checkpoint_internal_effects"):
        old, new = previous.get(key, {}), current.get(key, {})
        if not set(old) <= set(new) or any(new[k] != row for k, row in old.items()):
            raise ValueError("CHECKPOINT_ATTRIBUTION_HISTORY_REWRITE")
    internal = set(current.get("checkpoint_internal_effects", {})) - set(
        previous.get("checkpoint_internal_effects", {})
    )
    allocations = set(current.get("checkpoint_allocations", {})) - set(
        previous.get("checkpoint_allocations", {})
    )
    work = deepcopy(previous)
    if internal:
        if kind != "CHECKPOINT_RESERVE":
            raise ValueError("CHECKPOINT_INTERNAL_WITHOUT_COMMIT")
        decision_ids = {
            current["checkpoint_internal_effects"][k]["effects"]["decision_id"] for k in internal
        }
        if len(decision_ids) != 1:
            raise ValueError("CHECKPOINT_INTERNAL_BATCH")
        identity = next(iter(decision_ids))
        record = current["checkpoint_decisions"][identity]
        apply_internal(
            work,
            record,
            identity,
            current["checkpoint_internal_effects"][next(iter(internal))]["at_ms"],
        )
        if work.get("checkpoint_internal_effects") != current.get("checkpoint_internal_effects"):
            raise ValueError("CHECKPOINT_INTERNAL_REPLAY")
    if allocations:
        if kind != "RECONCILE":
            raise ValueError("CHECKPOINT_ATTRIBUTION_WITHOUT_RECONCILIATION")
        work["intents"] = deepcopy(previous["intents"])
        for execution_id in sorted(
            allocations, key=lambda k: (current["fills"][k]["known_at_ms"], k)
        ):
            fill = current["fills"][execution_id]
            if execution_id in previous["fills"]:
                raise ValueError("CHECKPOINT_ALLOCATION_DUPLICATE")
            intent = work["intents"][fill["intent_id"]]
            if intent.get("checkpoint_policy") != POLICY:
                raise ValueError("CHECKPOINT_ALLOCATION_POLICY")
            effects = apply_fill(
                work,
                fill,
                previous_filled=decimal(intent["filled"]),
                before_quantity=decimal(work["positions"].get(fill["market"], "0")),
            )
            work["positions"][fill["market"]] = str(
                add(decimal(work["positions"].get(fill["market"], "0")), decimal(fill["quantity"]))
            )
            intent["filled"] = str(add(decimal(intent["filled"]), decimal(fill["quantity"])))
            if (
                work["checkpoint_allocations"][execution_id]
                != current["checkpoint_allocations"][execution_id]
            ):
                raise ValueError("CHECKPOINT_FILL_ATTRIBUTION_REPLAY")
            for field, expected in (
                ("quantity", Fraction(decimal(fill["quantity"]))),
                (
                    "notional",
                    Fraction(decimal(fill["quantity"])) * Fraction(decimal(fill["price"])),
                ),
                ("fee", Fraction(decimal(fill["fee"]))),
                (
                    "cash_delta",
                    Fraction(work["checkpoint_allocations"][execution_id]["basis"]["realized"])
                    - Fraction(decimal(fill["fee"])),
                ),
            ):
                if sum((Fraction(row[field]) for row in effects.values()), Fraction(0)) != expected:
                    raise ValueError("CHECKPOINT_FILL_CONSERVATION")
    if (internal or allocations) and any(
        work.get(key, {}) != current.get(key, {})
        for key in ("pod_positions", "pod_fees_paid", "pod_realized", "checkpoint_basis")
    ):
        raise ValueError("CHECKPOINT_POD_LEDGER_CONSERVATION")
    for execution_id, fill in current.get("fills", {}).items():
        if current["intents"][fill["intent_id"]].get("checkpoint_policy") == POLICY:
            if execution_id not in current.get("checkpoint_allocations", {}):
                raise ValueError("CHECKPOINT_FILL_ATTRIBUTION_MISSING")
