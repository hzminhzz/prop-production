"""Exact pod attribution in the existing financial journal; no external executions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Literal

from copytrade_kernel.money import add as add
from copytrade_kernel.money import finite as finite

from .contracts import CHECKPOINT_POLICY_V3
from .evidence import decimal, digest

ZERO = Decimal(0)
POLICY = CHECKPOINT_POLICY_V3


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
    # Declared synthetic linear average-cost contract; native rounding remains unqualified.
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


def _funding_anchor(state: dict[str, Any]) -> str:
    return digest(
        {"pod_positions": state["pod_positions"], "pod_funding": state.get("pod_funding", {})}
    )


def apply_funding(
    state: dict[str, Any], event: dict[str, Any], *, expected_quantity: Decimal
) -> dict[str, str]:
    market = event["market"]
    amount = decimal(event["amount"])
    position_quantity = decimal(event["position_quantity"])
    if expected_quantity == ZERO or position_quantity != expected_quantity:
        raise ValueError("FUNDING_POD_BOOK_MISMATCH")
    weights = {
        pod: decimal(book.get(market, "0"))
        for pod, book in state["pod_positions"].items()
        if decimal(book.get(market, "0")) != ZERO
    }
    if not weights or add(*weights.values()) != expected_quantity:
        raise ValueError("FUNDING_POD_BOOK_MISMATCH")
    shares = split(amount, weights) if amount != ZERO else {pod: ZERO for pod in sorted(weights)}
    predecessor = _funding_anchor(state)
    funded = state.setdefault("pod_funding", {})
    pod_shares: dict[str, str] = {}
    for pod in sorted(shares):
        share = shares[pod]
        funded[pod] = str(add(decimal(funded.get(pod, "0")), share))
        pod_shares[pod] = str(share)
    state.setdefault("checkpoint_funding_allocations", {})[event["funding_id"]] = {
        "predecessor": predecessor,
        "event": "BROKER_FUNDING",
        "evidence_sha": digest(event),
        "at_ms": event["known_at_ms"],
        "actor": "GUARDIAN",
        "guard": "VERIFIED_POSITION_AND_POD_CONSERVATION",
        "successor": _funding_anchor(state),
        "market": market,
        "amount": str(amount),
        "position_quantity": str(position_quantity),
        "pod_shares": pod_shares,
    }
    return pod_shares


_FUNDING_EVENT_FIELDS = frozenset(
    {"funding_id", "market", "amount", "position_quantity", "known_at_ms"}
)


def _validate_funding_events_shape(events: Any) -> None:
    if not isinstance(events, dict):
        raise ValueError("FUNDING_EVENT_INVALID")
    for key, event in events.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(event, dict)
            or set(event) != _FUNDING_EVENT_FIELDS
            or event["funding_id"] != key
            or not isinstance(event["market"], str)
            or not event["market"]
            or type(event["known_at_ms"]) is not int
            or event["known_at_ms"] < 0
        ):
            raise ValueError("FUNDING_EVENT_INVALID")
        try:
            decimal(event["amount"])
            pos_qty = decimal(event["position_quantity"])
        except ValueError as exc:
            raise ValueError("FUNDING_EVENT_INVALID") from exc
        if pos_qty == ZERO:
            raise ValueError("FUNDING_EVENT_INVALID")


def _replay_new_fill(
    work: dict[str, Any],
    fill: dict[str, Any],
    execution_id: str,
    allocations: set[str],
    current: dict[str, Any],
) -> None:
    intent = work["intents"][fill["intent_id"]]
    if intent.get("checkpoint_policy") == POLICY:
        if execution_id not in allocations:
            raise ValueError("CHECKPOINT_FILL_ATTRIBUTION_MISSING")
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
        return
    if execution_id in allocations:
        raise ValueError("CHECKPOINT_ALLOCATION_POLICY")
    changes = {pod: decimal(change) for pod, change in intent.get("pod_changes", {}).items()}
    gross_changes = sum((abs(change) for change in changes.values()), ZERO)
    intent_qty = decimal(intent["quantity"])
    if (
        not changes
        or not gross_changes
        or not intent_qty
        or sum(changes.values(), ZERO) != intent_qty
    ):
        raise ValueError("POD_FILL_ALLOCATION_INVALID")
    qty_dec = decimal(fill["quantity"])
    fee_dec = decimal(fill["fee"])
    decimal(fill["price"])
    intent["filled"] = str(decimal(intent["filled"]) + qty_dec)
    work.setdefault("positions", {})[fill["market"]] = str(
        decimal(work.get("positions", {}).get(fill["market"], "0")) + qty_dec
    )
    for pod, change in changes.items():
        book = work.setdefault("pod_positions", {}).setdefault(pod, {})
        allocated = change * qty_dec / intent_qty
        book[fill["market"]] = str(decimal(book.get(fill["market"], "0")) + allocated)
        fee = fee_dec * abs(change) / gross_changes
        pod_fees = work.setdefault("pod_fees_paid", {})
        pod_fees[pod] = str(decimal(pod_fees.get(pod, "0")) + fee)


def validate_attribution(previous: dict[str, Any], current: dict[str, Any], kind: str) -> None:
    if "funding_events" in current:
        _validate_funding_events_shape(current["funding_events"])
    for key in (
        "checkpoint_allocations",
        "checkpoint_internal_effects",
        "checkpoint_funding_allocations",
        "funding_events",
    ):
        old, new = previous.get(key, {}), current.get(key, {})
        if (
            not isinstance(new, dict)
            or not set(old) <= set(new)
            or any(new[k] != row for k, row in old.items())
        ):
            raise ValueError("CHECKPOINT_ATTRIBUTION_HISTORY_REWRITE")
    internal = set(current.get("checkpoint_internal_effects", {})) - set(
        previous.get("checkpoint_internal_effects", {})
    )
    allocations = set(current.get("checkpoint_allocations", {})) - set(
        previous.get("checkpoint_allocations", {})
    )
    funding_allocations = set(current.get("checkpoint_funding_allocations", {})) - set(
        previous.get("checkpoint_funding_allocations", {})
    )
    new_fills = set(current.get("fills", {})) - set(previous.get("fills", {}))
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
    if allocations or funding_allocations:
        if kind != "RECONCILE":
            raise ValueError("CHECKPOINT_ATTRIBUTION_WITHOUT_RECONCILIATION")
        work["intents"] = deepcopy(previous.get("intents", {}))
        fills_cur = current.get("fills", {})
        events_cur = current.get("funding_events", {})
        for execution_id in allocations:
            if execution_id in previous.get("fills", {}):
                raise ValueError("CHECKPOINT_ALLOCATION_DUPLICATE")
            if execution_id not in fills_cur:
                raise ValueError("CHECKPOINT_ALLOCATION_FILL_MISSING")
        for funding_id in funding_allocations:
            if funding_id in previous.get("funding_events", {}):
                raise ValueError("CHECKPOINT_ALLOCATION_DUPLICATE")
            if funding_id not in events_cur:
                raise ValueError("CHECKPOINT_FUNDING_EVENT_MISSING")
        timeline: list[tuple[int, int, str]] = []
        for execution_id in new_fills:
            fill = fills_cur[execution_id]
            if (
                not isinstance(fill, dict)
                or fill.get("execution_id") != execution_id
                or fill.get("intent_id") not in work["intents"]
                or type(fill.get("known_at_ms")) is not int
                or any(k not in fill for k in ("market", "quantity", "price", "fee"))
            ):
                raise ValueError("GUARDIAN_SEMANTIC_ORPHAN_OR_REBOUND_FILL")
            timeline.append((fill["known_at_ms"], 0, execution_id))
        for funding_id in funding_allocations:
            timeline.append((events_cur[funding_id]["known_at_ms"], 1, funding_id))
        timeline.sort()
        for _ts, rank, item_id in timeline:
            if rank == 0:
                _replay_new_fill(work, fills_cur[item_id], item_id, allocations, current)
            else:
                event = events_cur[item_id]
                shares = apply_funding(
                    work,
                    event,
                    expected_quantity=decimal(work["positions"].get(event["market"], "0")),
                )
                if (
                    work["checkpoint_funding_allocations"][item_id]
                    != current["checkpoint_funding_allocations"][item_id]
                ):
                    raise ValueError("CHECKPOINT_FUNDING_ATTRIBUTION_REPLAY")
                if add(*(decimal(v) for v in shares.values())) != decimal(event["amount"]):
                    raise ValueError("CHECKPOINT_FUNDING_CONSERVATION")
    if (internal or allocations) and any(
        work.get(key, {}) != current.get(key, {})
        for key in (
            "pod_positions",
            "pod_fees_paid",
            "pod_realized",
            "checkpoint_basis",
        )
    ):
        raise ValueError("CHECKPOINT_POD_LEDGER_CONSERVATION")
    if funding_allocations and any(
        work.get(key, {}) != current.get(key, {})
        for key in ("pod_funding", "checkpoint_funding_allocations")
    ):
        raise ValueError("CHECKPOINT_POD_LEDGER_CONSERVATION")
    if not funding_allocations and previous.get("pod_funding", {}) != current.get(
        "pod_funding", {}
    ):
        raise ValueError("CHECKPOINT_POD_LEDGER_CONSERVATION")
    for funding_id in current.get("funding_events", {}):
        if funding_id not in current.get("checkpoint_funding_allocations", {}):
            raise ValueError("CHECKPOINT_FUNDING_ATTRIBUTION_MISSING")
    for execution_id, fill in current.get("fills", {}).items():
        intent = current["intents"].get(fill["intent_id"])
        if intent is None:
            raise ValueError("GUARDIAN_SEMANTIC_ORPHAN_OR_REBOUND_FILL")
        if intent.get("checkpoint_policy") == POLICY:
            if execution_id not in current.get("checkpoint_allocations", {}):
                raise ValueError("CHECKPOINT_FILL_ATTRIBUTION_MISSING")
