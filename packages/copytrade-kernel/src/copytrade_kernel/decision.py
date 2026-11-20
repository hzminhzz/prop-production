"""Immutable exact economics passed to the pure checkpoint planner."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from .clocks import DecisionMs
from .execution import project
from .money import add, decimal
from .risk import size_book

type Scalar = str | int | bool | None


@dataclass(frozen=True, slots=True)
class FrozenObject:
    fields: tuple[tuple[str, FrozenValue], ...]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> FrozenObject:
        return cls(tuple((key, freeze(value)) for key, value in data.items()))

    def data(self) -> dict[str, Any]:
        return {key: thaw(value) for key, value in self.fields}


type FrozenValue = Scalar | FrozenObject | tuple[FrozenValue, ...]


def freeze(value: object) -> FrozenValue:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("KERNEL_STRING_KEYS_REQUIRED")
        return FrozenObject.parse(value)
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ValueError("KERNEL_EXACT_VALUE_REQUIRED")


def thaw(value: FrozenValue) -> Any:
    if isinstance(value, FrozenObject):
        return value.data()
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class VenueCapability:
    market: str
    cross_zero_verified: bool
    reduce_only_verified: bool


@dataclass(frozen=True, slots=True)
class MarketContext:
    quotes: FrozenObject
    markets: FrozenObject
    capabilities: tuple[VenueCapability, ...]


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    fields: FrozenObject


@dataclass(frozen=True, slots=True)
class AccountState:
    finance: FrozenObject
    positions: FrozenObject
    owner_positions: FrozenObject
    ledger_basis: FrozenObject
    observed_basis: FrozenObject


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    account: AccountState
    market: MarketContext
    policy: RiskPolicy
    now_ms: DecisionMs


@dataclass(frozen=True, slots=True)
class NetOrderIntent:
    market: str
    net_delta_qty: Decimal
    reduce_only: bool
    owner_allocations: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    risk: FrozenObject
    execution_plan: FrozenObject | None
    projection_error: str | None
    net_orders: tuple[NetOrderIntent, ...]


def plan_execution(
    notionals: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
    inputs: DecisionInputs,
) -> ExecutionDecision:
    account, market = inputs.account, inputs.market
    positions = cast(dict[str, str], account.positions.data())
    owner_positions = cast(dict[str, dict[str, str]], account.owner_positions.data())
    selected = {owner for owner, _ in notionals}
    frozen = {owner: book for owner, book in owner_positions.items() if owner not in selected}
    pods = [{"pod_id": owner, "requested_notionals": dict(book)} for owner, book in notionals]
    pods.extend({"pod_id": owner, "requested_notionals": {}} for owner in frozen)
    pods.sort(key=lambda pod: cast(str, pod["pod_id"]))
    quotes, markets = market.quotes.data(), market.markets.data()
    risk = size_book(
        pods,
        quotes,
        markets,
        inputs.policy.fields.data(),
        account.finance.data(),
        positions,
        owner_positions,
        [],
        now_ms=inputs.now_ms.value,
        quantity_price_policy="EXECUTABLE_SIDE_V3",
        frozen_pods=frozen,
    )
    frozen_risk = FrozenObject.parse(risk)
    if risk["status"] != "ACCEPTED":
        return ExecutionDecision(frozen_risk, None, None, ())
    capabilities = {cap.market: cap for cap in market.capabilities}

    def permits(symbol: str, feature: str) -> bool:
        cap = capabilities.get(symbol)
        if cap is None:
            return False
        if feature == "cross_zero":
            return cap.cross_zero_verified
        if feature == "reduce_only":
            return cap.reduce_only_verified
        raise ValueError("KERNEL_UNKNOWN_CAPABILITY")

    try:
        plan = project(
            {
                "positions": positions,
                "pod_positions": owner_positions,
                "checkpoint_basis": account.ledger_basis.data(),
            },
            risk,
            {"position_basis": account.observed_basis.data()},
            markets,
            quotes,
            frozen,
            permits=permits,
        )
    except ValueError as exc:
        return ExecutionDecision(frozen_risk, None, str(exc), ())
    orders = []
    for symbol, target in sorted(plan["targets"].items()):
        delta = add(decimal(target), -decimal(positions.get(symbol, "0")))
        if delta:
            allocations = tuple(
                (
                    owner,
                    add(
                        decimal(book.get(symbol, "0")),
                        -decimal(owner_positions.get(owner, {}).get(symbol, "0")),
                    ),
                )
                for owner, book in sorted(plan["pod_targets"].items())
            )
            orders.append(NetOrderIntent(symbol, delta, plan["reduce_only"][symbol], allocations))
    return ExecutionDecision(frozen_risk, FrozenObject.parse(plan), None, tuple(orders))
