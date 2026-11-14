"""Provider-neutral evidence and mutation outcomes. Remote implementations are read-only."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import quote

from .contracts import SYNTHETIC_ACCOUNT, SYNTHETIC_ENDPOINT
from .evidence import decimal, digest
from .execution_capabilities import permits


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
    raw_status: str | None = None
    normalization_schema: str | None = None
    normalization_provenance: str | None = None


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
    market_marks: tuple[tuple[str, Decimal, int], ...] = ()
    provider_balance: Decimal | None = None
    provider_buying_power: Decimal | None = None
    provider_financial_provenance: tuple[tuple[str, str], ...] = ()
    provider_time: str | None = None
    native_account_id: str | None = None
    native_attempt_id: str | None = None
    native_account_source: str | None = None
    evidence_schema: str | None = None
    uncertainty_reasons: tuple[str, ...] = ()
    native_inventory_read_complete: bool = False
    native_inventory_count: tuple[int, int, int] | None = None
    execution_capabilities: tuple[dict[str, Any], ...] = ()

    def ready(self, now_ms: int, max_age_ms: int) -> bool:
        amounts = (
            self.cash,
            self.equity,
            self.unrealized,
            self.initial_margin,
            self.maintenance_margin,
        )
        if any(not isinstance(v, Decimal) or not v.is_finite() for v in amounts):
            return False
        if any(type(v) is not int for v in (self.epoch, self.observed_at_ms, self.received_at_ms)):
            return False
        for market, price, known_ms in self.market_marks:
            if (
                not market
                or not isinstance(price, Decimal)
                or not price.is_finite()
                or price <= 0
                or type(known_ms) is not int
                or not 0 <= self.observed_at_ms - known_ms <= 5000
            ):
                return False
        for position in self.positions:
            if (
                not position.market
                or any(
                    not isinstance(v, Decimal) or not v.is_finite()
                    for v in (position.quantity, position.entry_price, position.mark_price)
                )
                or position.entry_price < 0
                or position.mark_price <= 0
            ):
                return False
        for order in self.orders:
            if (
                not order.intent_id
                or not order.broker_id
                or not order.market
                or any(
                    not isinstance(v, Decimal) or not v.is_finite()
                    for v in (order.quantity, order.filled)
                )
                or order.quantity == 0
                or abs(order.filled) > abs(order.quantity)
                or order.filled * order.quantity < 0
                or type(order.reduce_only) is not bool
            ):
                return False
        for fill in self.fills:
            if (
                not fill.execution_id
                or not fill.intent_id
                or not fill.market
                or any(
                    not isinstance(v, Decimal) or not v.is_finite()
                    for v in (fill.quantity, fill.price, fill.fee)
                )
                or fill.quantity == 0
                or fill.price <= 0
                or fill.fee < 0
                or type(fill.known_at_ms) is not int
                or not 0 <= fill.known_at_ms <= self.observed_at_ms
            ):
                return False
        return bool(
            self.connected is True
            and self.financial_complete is True
            and self.history_complete is True
            and isinstance(self.cutoff, str)
            and self.cutoff
            and self.epoch > 0
            and 0 <= self.observed_at_ms <= self.received_at_ms <= now_ms
            and now_ms - self.observed_at_ms <= max_age_ms
            and self.cash is not None
            and self.unrealized is not None
            and self.equity == self.cash + self.unrealized
            and self.initial_margin is not None
            and self.initial_margin >= 0
            and self.maintenance_margin is not None
            and self.maintenance_margin >= 0
        )


@dataclass(frozen=True)
class Outcome:
    status: str
    broker_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CanonicalBrokerStatus:
    schema: str
    provider: str
    raw_status: str
    status: str
    provenance: str


def normalize_broker_status(
    provider: str,
    raw_status: str,
    *,
    quantity: Decimal,
    cumulative_filled: Decimal,
    complete_history: bool,
    executions_reconciled: bool,
) -> CanonicalBrokerStatus:
    """Map the provider's label without creating absent history or finance proof.

    A provider FILLED is not a terminal financial fact until actual executions
    conserve the full signed order quantity at an independently complete cutoff.
    """
    if provider not in ("propr", "bybit_demo"):
        raise ValueError("BROKER_PROVIDER_NORMALIZER_NOT_PINNED")
    if (
        not isinstance(raw_status, str)
        or not raw_status.strip()
        or quantity == 0
        or not quantity.is_finite()
        or not cumulative_filled.is_finite()
        or cumulative_filled * quantity < 0
        or abs(cumulative_filled) > abs(quantity)
    ):
        raise ValueError("BROKER_NORMALIZATION_INPUT_INVALID")
    key = raw_status.strip().lower()
    # Provider-specific source vocabulary belongs here, not in Guardian.
    if provider == "propr":
        nonterminal = {"open": "ACCEPTED", "partially_filled": "PARTIAL", "partial": "PARTIAL"}
        terminal = {
            "filled": "FILLED",
            "cancelled": "CANCELLED",
            "canceled": "CANCELLED",
            "expired": "EXPIRED",
            "rejected": "REJECTED",
        }
    else:
        nonterminal = {"open": "ACCEPTED", "partially_filled": "PARTIAL"}
        terminal = {
            "filled": "FILLED",
            "cancelled": "CANCELLED",
            "canceled": "CANCELLED",
            "expired": "EXPIRED",
            "rejected": "REJECTED",
        }
    if key in nonterminal:
        canonical_status = nonterminal[key]
        provenance = "OBSERVED_OPEN_NOT_TERMINAL_PROOF"
    elif key in terminal:
        if not complete_history or not executions_reconciled:
            canonical_status = "UNKNOWN"
            provenance = "TERMINAL_EVIDENCE_UNQUALIFIED"
        elif terminal[key] == "FILLED" and cumulative_filled != quantity:
            canonical_status = "UNKNOWN"
            provenance = "FALSE_FILLED_TERMINAL"
        elif terminal[key] == "REJECTED" and cumulative_filled != 0:
            canonical_status = "UNKNOWN"
            provenance = "REJECTED_WITH_ACTUAL_EXECUTIONS"
        else:
            canonical_status = terminal[key]
            provenance = "PROVIDER_CUTOFF_AND_EXECUTIONS_VERIFIED"
    else:
        canonical_status = "UNKNOWN"
        provenance = "UNKNOWN_PROVIDER_STATUS"
    return CanonicalBrokerStatus(
        schema="broker_status_normalization_v1",
        provider=provider,
        raw_status=raw_status,
        status=canonical_status,
        provenance=provenance,
    )


def execution_capability(
    raw: dict[str, Any],
    feature: str,
    *,
    account: str,
    endpoint: str,
    instrument: str,
    now_ms: int,
    synthetic: bool,
) -> bool:
    """Validate the pinned connector evidence contract without granting a permit."""
    return permits(
        raw, feature, account=account, endpoint=endpoint, instrument=instrument,
        now_ms=now_ms, synthetic=synthetic,
    )


class BrokerGateway(Protocol):
    synthetic: bool
    endpoint: str

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

    positions = tuple(
        Position(
            p["market"], decimal(p["quantity"]), decimal(p["entry_price"]), decimal(p["mark_price"])
        )
        for p in raw["positions"]
    )
    orders = tuple(
        BrokerOrder(
            o["intent_id"],
            o["broker_id"],
            o["market"],
            decimal(o["quantity"]),
            decimal(o["filled"]),
            o["status"],
            o["reduce_only"],
            o.get("raw_status"),
            o.get("normalization_schema"),
            o.get("normalization_provenance"),
        )
        for o in raw["orders"]
    )
    fills = tuple(
        Execution(
            f["execution_id"],
            f["intent_id"],
            f["market"],
            decimal(f["quantity"]),
            decimal(f["price"]),
            decimal(f["fee"]),
            f["known_at_ms"],
        )
        for f in raw["fills"]
    )
    return BrokerSnapshot(
        raw["account_binding"],
        raw["endpoint"],
        raw["observed_at_ms"],
        raw["received_at_ms"],
        raw["epoch"],
        amount("cash"),
        amount("equity"),
        amount("unrealized"),
        amount("initial_margin"),
        amount("maintenance_margin"),
        positions,
        orders,
        fills,
        raw["cutoff"],
        raw["history_complete"],
        raw["connected"],
        raw["financial_complete"],
        tuple((m, decimal(price), known) for m, price, known in raw.get("market_marks", [])),
        amount("provider_balance"),
        amount("provider_buying_power"),
        tuple(tuple(field) for field in raw.get("provider_financial_provenance", ())),
        raw.get("provider_time"),
        raw.get("native_account_id"),
        raw.get("native_attempt_id"),
        raw.get("native_account_source"),
        raw.get("evidence_schema"),
        tuple(raw.get("uncertainty_reasons", ())),
        raw.get("native_inventory_read_complete", False),
        tuple(raw["native_inventory_count"])
        if raw.get("native_inventory_count") is not None
        else None,
        tuple(raw.get("execution_capabilities", ())),
    )


def fixture_snapshot(now_ms: int = 1000) -> BrokerSnapshot:
    return BrokerSnapshot(
        SYNTHETIC_ACCOUNT,
        SYNTHETIC_ENDPOINT,
        now_ms,
        now_ms,
        1,
        Decimal("10000"),
        Decimal("10000"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        cutoff="fixture-cut-0",
        history_complete=True,
        connected=True,
        financial_complete=True,
        market_marks=(("BTC", Decimal("10000"), now_ms), ("ETH", Decimal("1000"), now_ms)),
    )


async def inspect_propr_readonly(
    rest: Any, account_id: str, endpoint: str, received_at_ms: int
) -> BrokerSnapshot:
    """Consume versioned provider evidence; never reconstruct Guardian cash.

    Diagnostic inventories are not coherent broker history. Synthetic fixtures
    continue to use their own fully-proven fake history and accounting path.
    """
    native = importlib.import_module("connectors.propr.native_evidence")
    evidence = await native.inspect_native_evidence(
        rest,
        account_id=account_id,
        endpoint=endpoint,
        known_at_ms=received_at_ms,
        stream_connected=False,
    )
    if evidence.schema != "propr_native_evidence_v1":
        raise ValueError("NATIVE_EVIDENCE_VERSION_UNSUPPORTED")
    if evidence.endpoint != endpoint or evidence.account_id != account_id:
        raise ValueError("NATIVE_EVIDENCE_SCOPE_MISMATCH")

    financial = evidence.financial
    # No provider-verified cash is available. Never satisfy Guardian's synthetic
    # cash+unrealized=equity equation with a fabricated residual.
    positions: tuple[Position, ...] = ()
    orders: tuple[BrokerOrder, ...] = ()
    fills: tuple[Execution, ...] = ()
    additional_reasons: list[str] = []
    if evidence.inventory is not None:
        entries: list[Position] = []
        for item in evidence.inventory.positions:
            if item.entry_price is None or item.mark_price is None:
                additional_reasons.append("POSITION_PRICE_UNVERIFIED")
                entries.clear()
                break
            entries.append(
                Position(
                    item.asset,
                    item.quantity if item.side == "long" else -item.quantity,
                    item.entry_price,
                    item.mark_price,
                )
            )
        positions = tuple(entries)
        orders = tuple(
            BrokerOrder(
                item.intent_id or "",
                item.id,
                item.asset,
                item.quantity if item.side == "buy" else -item.quantity,
                item.filled if item.side == "buy" else -item.filled,
                (
                    normalized := normalize_broker_status(
                        "propr",
                        item.status.value,
                        quantity=item.quantity if item.side == "buy" else -item.quantity,
                        cumulative_filled=item.filled if item.side == "buy" else -item.filled,
                        complete_history=evidence.history_complete,
                        executions_reconciled=False,  # current REST inventory is not a causal cutoff
                    )
                ).status,
                item.reduce_only is True,
                normalized.raw_status,
                normalized.schema,
                normalized.provenance,
            )
            for item in evidence.inventory.orders
        )
        order_intents = {item.id: item.intent_id for item in evidence.inventory.orders}
        mapped_fills: list[Execution] = []
        for trade in evidence.inventory.trades:
            intent = order_intents.get(trade.order_id)
            if (
                not intent
                or trade.fee is None
                or trade.side not in ("buy", "sell")
                or not evidence.provider_time
            ):
                additional_reasons.append("EXECUTION_EVIDENCE_UNVERIFIED")
                mapped_fills.clear()
                break
            # Provider execution timestamps are native strings; no assertion that
            # these give a coherent history or independently witnessed fill cutoff.
            try:
                parsed = datetime.fromisoformat(trade.timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("EXECUTION_TIME_UNVERIFIED")
                execution_time = int(parsed.timestamp() * 1000)
            except (ValueError, OverflowError):
                additional_reasons.append("EXECUTION_TIME_UNVERIFIED")
                mapped_fills.clear()
                break
            mapped_fills.append(
                Execution(
                    trade.id,
                    intent,
                    trade.asset,
                    trade.quantity if trade.side == "buy" else -trade.quantity,
                    trade.price,
                    trade.fee,
                    execution_time,
                )
            )
        fills = tuple(mapped_fills)
    provenance = tuple(
        (name, getattr(financial, name).provenance)
        for name in (
            "balance",
            "equity",
            "buying_power",
            "cash",
            "unrealized_pnl",
            "initial_margin",
            "maintenance_margin",
        )
    )
    return BrokerSnapshot(
        account_binding=digest({"provider": "propr", "endpoint": endpoint, "account": account_id}),
        endpoint=endpoint,
        observed_at_ms=received_at_ms,
        received_at_ms=received_at_ms,
        epoch=0,
        cash=financial.cash.value,
        equity=financial.equity.value,
        unrealized=financial.unrealized_pnl.value,
        initial_margin=financial.initial_margin.value,
        maintenance_margin=financial.maintenance_margin.value,
        positions=positions,
        orders=orders,
        fills=fills,
        cutoff=evidence.cutoff,
        history_complete=evidence.history_complete,
        connected=evidence.stream_connected,
        financial_complete=False,
        provider_balance=financial.balance.value,
        provider_buying_power=financial.buying_power.value,
        provider_financial_provenance=provenance,
        provider_time=evidence.provider_time,
        native_account_id=evidence.account_id if evidence.account_bound else None,
        native_attempt_id=evidence.attempt_id,
        native_account_source=evidence.account_source,
        evidence_schema=evidence.schema,
        uncertainty_reasons=tuple(sorted(set(evidence.reasons) | set(additional_reasons))),
        native_inventory_read_complete=evidence.inventory_read_complete,
        native_inventory_count=(
            len(evidence.inventory.orders),
            len(evidence.inventory.positions),
            len(evidence.inventory.trades),
        )
        if evidence.inventory is not None
        else None,
    )


async def inspect_bybit_demo_readonly(client: Any, received_at_ms: int) -> BrokerSnapshot:
    """Map the pinned connector's authenticated Bybit Demo reads into SHADOW evidence.

    The existing connector owns all Bybit V5 and CCXT protocol calls. A completed
    REST observation cannot prove a coherent replay boundary, trade history or
    ledger cash, and never permits Guardian openings.
    """
    config = getattr(client, "config", None)
    if config is None:
        raise PermissionError("BYBIT_DEMO_SCOPE_UNVERIFIED")
    environment = getattr(getattr(config, "environment", None), "value", None)
    mode = getattr(getattr(config, "mode", None), "value", None)
    identity = getattr(client, "demo_identity_evidence", {})
    if (
        environment != "demo"
        or mode != "read_only"
        or getattr(client, "demo_identity_verified", False) is not True
        or not isinstance(identity, dict)
        or identity.get("environment") != "demo"
        or identity.get("private_rest") != "api-demo.bybit.com"
        or identity.get("authenticated_uid_matches_expected") is not True
        or identity.get("dedicated_demo_key_matches") is not True
        or identity.get("unified_account_verified") is not True
        or not getattr(config, "expected_demo_uid", None)
    ):
        raise PermissionError("BYBIT_DEMO_SCOPE_UNVERIFIED")
    if type(received_at_ms) is not int or received_at_ms <= 0:
        raise ValueError("BYBIT_OBSERVATION_TIME_INVALID")

    summary = await client.fetch_demo_account_summary()
    positions_native = await client.fetch_demo_positions()
    orders_native = await client.fetch_demo_open_orders()
    equity = decimal(summary.equity_usd)
    buying_power = decimal(summary.available_usd)
    if not equity.is_finite() or not buying_power.is_finite():
        raise ValueError("BYBIT_FINANCIAL_EVIDENCE_INVALID")

    positions: list[Position] = []
    seen_markets: set[str] = set()
    for item in positions_native:
        size = decimal(item.size)
        if (
            item.side not in ("buy", "sell")
            or size <= 0
            or not isinstance(item.symbol, str)
            or not item.symbol
            or item.symbol in seen_markets
            or item.entry_price is None
            or item.mark_price is None
        ):
            raise ValueError("BYBIT_POSITION_EVIDENCE_INVALID")
        entry, mark = decimal(item.entry_price), decimal(item.mark_price)
        if entry <= 0 or mark <= 0:
            raise ValueError("BYBIT_POSITION_PRICE_INVALID")
        positions.append(Position(item.symbol, size if item.side == "buy" else -size, entry, mark))
        seen_markets.add(item.symbol)

    orders: list[BrokerOrder] = []
    seen_orders: set[str] = set()
    for item in orders_native:
        size, filled = decimal(item.quantity), decimal(item.filled_quantity)
        if (
            item.side not in ("buy", "sell")
            or item.status not in ("open", "partially_filled")
            or not isinstance(item.order_id, str)
            or not item.order_id
            or item.order_id in seen_orders
            or not item.symbol
            or size <= 0
            or filled < 0
            or filled > size
        ):
            raise ValueError("BYBIT_ORDER_EVIDENCE_INVALID")
        sign = Decimal("1") if item.side == "buy" else Decimal("-1")
        normalized = normalize_broker_status(
            "bybit_demo",
            item.status,
            quantity=size * sign,
            cumulative_filled=filled * sign,
            complete_history=False,
            executions_reconciled=False,
        )
        orders.append(
            BrokerOrder(
                item.client_order_id or "",
                item.order_id,
                item.symbol,
                size * sign,
                filled * sign,
                normalized.status,
                False,  # reduce-only is not proven by this connector's order DTO
                normalized.raw_status,
                normalized.schema,
                normalized.provenance,
            )
        )
        seen_orders.add(item.order_id)

    # Avoid storing UID or key in evidence. The preflight has authenticated
    # exact UID/key equality, and this fingerprint scopes the read-only account.
    scope = digest({"venue": "bybit", "environment": "demo", "uid": config.expected_demo_uid})
    return BrokerSnapshot(
        account_binding=scope,
        endpoint="https://api-demo.bybit.com",
        observed_at_ms=received_at_ms,
        received_at_ms=received_at_ms,
        epoch=0,
        cash=None,
        equity=equity,
        unrealized=None,
        initial_margin=None,
        maintenance_margin=None,
        positions=tuple(positions),
        orders=tuple(orders),
        fills=(),
        cutoff=None,
        history_complete=False,
        connected=False,
        financial_complete=False,
        provider_balance=None,
        provider_buying_power=buying_power,
        provider_financial_provenance=(
            ("equity", "PROVIDER_TOTAL_EQUITY_USD"),
            ("buying_power", "PROVIDER_TOTAL_AVAILABLE_BALANCE_USD"),
            ("cash", "UNKNOWN"),
            ("balance", "UNKNOWN"),
            ("unrealized_pnl", "UNKNOWN"),
            ("initial_margin", "UNKNOWN"),
            ("maintenance_margin", "UNKNOWN"),
        ),
        provider_time=None,
        native_account_id=None,
        native_account_source="bybit_demo",
        evidence_schema="bybit_demo_snapshot_v1",
        uncertainty_reasons=(
            "BYBIT_NO_VERIFIED_HISTORY_CUTOFF",
            "BYBIT_FINANCIAL_LEDGER_CASH_UNVERIFIED",
            "BYBIT_MARGIN_AND_UNREALIZED_PNL_UNVERIFIED",
            "BYBIT_EXECUTION_AND_FUNDING_UNVERIFIED",
            "BYBIT_PRIVATE_STREAM_NOT_ESTABLISHED",
            "REMOTE_MUTATION_UNAVAILABLE",
        ),
        # Position/order reads are complete for the supported V5 scope;
        # the all-history trade/fill inventory was not fetched.
        native_inventory_read_complete=False,
        native_inventory_count=None,
    )


def rest_account_path(account_id: str) -> str:
    if not account_id or "/" in account_id:
        raise ValueError("INVALID_ACCOUNT")
    return "/accounts/" + quote(account_id, safe="")
