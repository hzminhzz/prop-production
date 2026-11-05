"""Provider-neutral evidence and mutation outcomes. Remote implementations are read-only."""
from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .evidence import decimal, digest


@dataclass(frozen=True)
class Position:
    market: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal


@dataclass(frozen=True)
class BrokerOrder:
    intent_id: str
    broker_id: str
    market: str
    quantity: Decimal
    filled: Decimal
    status: str
    reduce_only: bool = False


@dataclass(frozen=True)
class Execution:
    execution_id: str
    intent_id: str
    market: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    known_at_ms: int


@dataclass(frozen=True)
class BrokerSnapshot:
    account_binding: str
    endpoint: str
    observed_at_ms: int
    received_at_ms: int
    epoch: int
    cash: Decimal | None
    equity: Decimal | None
    unrealized: Decimal | None
    initial_margin: Decimal | None
    maintenance_margin: Decimal | None
    positions: tuple[Position, ...] = ()
    orders: tuple[BrokerOrder, ...] = ()
    fills: tuple[Execution, ...] = ()
    cutoff: str | None = None
    history_complete: bool = False
    connected: bool = False
    financial_complete: bool = False

    def ready(self, now_ms: int, max_age_ms: int) -> bool:
        amounts = (self.cash, self.equity, self.unrealized,
                   self.initial_margin, self.maintenance_margin)
        if any(v is None or not v.is_finite() for v in amounts):
            return False
        return bool(
            self.connected and self.financial_complete and self.history_complete and self.cutoff
            and self.epoch > 0
            and 0 <= self.observed_at_ms <= self.received_at_ms <= now_ms
            and now_ms - self.observed_at_ms <= max_age_ms
            and self.cash is not None and self.unrealized is not None
            and self.equity == self.cash + self.unrealized
            and self.initial_margin is not None and self.initial_margin >= 0
            and self.maintenance_margin is not None and self.maintenance_margin >= 0
        )


@dataclass(frozen=True)
class Outcome:
    status: str
    broker_id: str | None = None
    reason: str | None = None


class BrokerGateway(Protocol):
    synthetic: bool

    def observe(self, now_ms: int) -> BrokerSnapshot: ...
    def submit(self, intent: dict[str, Any]) -> Outcome: ...
    def cancel(self, intent_id: str) -> Outcome: ...


class ReadOnlyGateway:
    synthetic = False

    def __init__(self, reader: Callable[[], BrokerSnapshot], account_binding: str, endpoint: str):
        self.reader = reader
        self.account_binding = account_binding
        self.endpoint = endpoint

    def observe(self, now_ms: int) -> BrokerSnapshot:
        snapshot = self.reader()
        if snapshot.account_binding != self.account_binding:
            raise ValueError("ACCOUNT_BINDING_MISMATCH")
        if snapshot.endpoint != self.endpoint:
            raise ValueError("ENDPOINT_BINDING_MISMATCH")
        return snapshot

    def submit(self, intent: dict[str, Any]) -> Outcome:
        raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")

    def cancel(self, intent_id: str) -> Outcome:
        raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")


def snapshot_from_wire(raw: dict[str, Any]) -> BrokerSnapshot:
    def amount(key: str) -> Decimal | None:
        return decimal(raw[key]) if raw.get(key) is not None else None

    positions = tuple(Position(p["market"], decimal(p["quantity"]), decimal(p["entry_price"]),
                               decimal(p["mark_price"])) for p in raw["positions"])
    orders = tuple(BrokerOrder(o["intent_id"], o["broker_id"], o["market"], decimal(o["quantity"]),
                               decimal(o["filled"]), o["status"], o["reduce_only"])
                   for o in raw["orders"])
    fills = tuple(Execution(f["execution_id"], f["intent_id"], f["market"], decimal(f["quantity"]),
                            decimal(f["price"]), decimal(f["fee"]), f["known_at_ms"])
                  for f in raw["fills"])
    return BrokerSnapshot(raw["account_binding"], raw["endpoint"], raw["observed_at_ms"],
                          raw["received_at_ms"], raw["epoch"], amount("cash"), amount("equity"),
                          amount("unrealized"), amount("initial_margin"),
                          amount("maintenance_margin"), positions, orders, fills, raw["cutoff"],
                          raw["history_complete"], raw["connected"], raw["financial_complete"])


def fixture_snapshot(now_ms: int = 1000) -> BrokerSnapshot:
    return BrokerSnapshot("fixture-account", "fake://broker", now_ms, now_ms, 1,
                          Decimal("10000"), Decimal("10000"), Decimal("0"), Decimal("0"),
                          Decimal("0"), cutoff="fixture-cut-0", history_complete=True,
                          connected=True, financial_complete=True)


async def inspect_propr_readonly(rest: Any, account_id: str, endpoint: str,
                                received_at_ms: int) -> BrokerSnapshot:
    """Uses the pinned connector's actual published read-only parsers.

    Offset pagination and ordinary REST timestamps do not prove a coherent cutoff.
    Even complete fields therefore remain non-ready; no invented replay sequence.
    """
    account = importlib.import_module("connectors.propr.account")
    inventory_module = importlib.import_module("connectors.propr.inventory")
    binding = await account.discover_account(rest, account_id)
    raw = await rest.request("GET", rest_account_path(account_id))
    financial = account.parse_account(account_id, binding.attempt_id, raw)
    inventory = await inventory_module.InventoryReader(rest, account_id).snapshot()
    positions: list[Position] = []
    complete = financial.complete
    for p in inventory.positions:
        if p.entry_price is None or p.mark_price is None:
            complete = False
            continue
        qty = p.quantity if p.side == "long" else -p.quantity
        positions.append(Position(p.asset, qty, p.entry_price, p.mark_price))
    orders = tuple(BrokerOrder(o.intent_id or "", o.id, o.asset,
                               o.quantity if o.side == "buy" else -o.quantity,
                               o.filled, str(o.status), bool(o.reduce_only))
                   for o in inventory.orders)
    cash = (financial.equity - financial.unrealized_pnl
            if financial.equity is not None and financial.unrealized_pnl is not None else None)
    return BrokerSnapshot(digest({"provider": "propr", "endpoint": endpoint, "account": account_id}),
                          endpoint, received_at_ms, received_at_ms, 1, cash, financial.equity,
                          financial.unrealized_pnl, financial.initial_margin,
                          financial.maintenance_margin, tuple(positions), orders,
                          cutoff=None, history_complete=False, connected=True,
                          financial_complete=complete)


def rest_account_path(account_id: str) -> str:
    from urllib.parse import quote
    if not account_id or "/" in account_id:
        raise ValueError("INVALID_ACCOUNT")
    return "/accounts/" + quote(account_id, safe="")
