"""CCXT-backed Bybit Demo reconciliation adapter and BrokerGateway implementation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from decimal import Decimal
from typing import Any, TypeVar

from .evidence import decimal, digest
from .gateway import (
    BrokerOrder,
    BrokerSnapshot,
    Execution,
    Outcome,
    Position,
    normalize_broker_status,
)

BYBIT_DEMO_ENDPOINT = "https://api-demo.bybit.com"
_ZERO = Decimal("0")
_T = TypeVar("_T")


def bybit_demo_account_binding(expected_demo_uid: str) -> str:
    """Return the canonical Guardian account_binding digest for a verified Bybit Demo UID."""
    uid = expected_demo_uid.strip()
    if not uid:
        raise ValueError("BYBIT_DEMO_UID_REQUIRED")
    return digest({"venue": "bybit", "environment": "demo", "uid": uid})


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = decimal(value)
    except Exception:
        return None
    return dec if dec.is_finite() else None


def snapshot_from_bybit_reconciliation(
    reconciled: Any,
    *,
    expected_demo_uid: str,
) -> BrokerSnapshot:
    """Convert a connector BybitDemoReconciliationSnapshot into a Guardian BrokerSnapshot."""
    binding = bybit_demo_account_binding(expected_demo_uid)
    endpoint = str(getattr(reconciled, "endpoint", "") or "")
    if endpoint != BYBIT_DEMO_ENDPOINT:
        raise ValueError("BYBIT_ENDPOINT_NOT_DEMO")

    reasons: list[str] = list(getattr(reconciled, "uncertainty_reasons", ()) or ())
    account = getattr(reconciled, "account", None)
    cash = _finite_decimal(getattr(account, "wallet_balance_usd", None)) if account else None
    equity = _finite_decimal(getattr(account, "equity_usd", None)) if account else None
    unrealized = _finite_decimal(getattr(account, "unrealized_pnl_usd", None)) if account else None
    initial_margin = _finite_decimal(getattr(account, "initial_margin_usd", None)) if account else None
    maintenance_margin = (
        _finite_decimal(getattr(account, "maintenance_margin_usd", None)) if account else None
    )
    buying_power = _finite_decimal(getattr(account, "available_usd", None)) if account else None

    financial_complete = bool(getattr(reconciled, "financial_complete", False))
    if any(
        v is None
        for v in (cash, equity, unrealized, initial_margin, maintenance_margin, buying_power)
    ):
        financial_complete = False
        if "BYBIT_UNIFIED_FINANCE_INCOMPLETE" not in reasons:
            reasons.append("BYBIT_UNIFIED_FINANCE_INCOMPLETE")
    elif (
        equity != cash + unrealized  # type: ignore[operator]
        and "UNIFIED_FINANCE_CONSERVATION_MISMATCH" not in reasons
    ):
        reasons.append("UNIFIED_FINANCE_CONSERVATION_MISMATCH")

    marks: list[tuple[str, Decimal, int]] = []
    mark_price_by_symbol: dict[str, Decimal] = {}
    for sym, price_raw, known_ms in getattr(reconciled, "market_marks", ()) or ():
        price_dec = _finite_decimal(price_raw)
        if isinstance(sym, str) and sym and price_dec is not None and type(known_ms) is int:
            marks.append((sym, price_dec, known_ms))
            mark_price_by_symbol[sym] = price_dec

    positions: list[Position] = []
    seen_pos_symbols: set[str] = set()
    for pos in getattr(reconciled, "positions", ()) or ():
        side = getattr(pos, "side", None)
        symbol = str(getattr(pos, "symbol", "") or "")
        size = _finite_decimal(getattr(pos, "size", None))
        entry = _finite_decimal(getattr(pos, "entry_price", None))
        mark = _finite_decimal(getattr(pos, "mark_price", None))
        if (
            side not in ("buy", "sell")
            or not symbol
            or symbol in seen_pos_symbols
            or size is None
            or size <= 0
            or entry is None
            or entry < 0
            or mark is None
            or mark <= 0
        ):
            raise ValueError("BYBIT_POSITION_EVIDENCE_INVALID")
        if symbol in mark_price_by_symbol and mark_price_by_symbol[symbol] != mark:
            if "BROKER_MARK_CONFLICT" not in reasons:
                reasons.append("BROKER_MARK_CONFLICT")
        signed_qty = size if side == "buy" else -size
        positions.append(Position(symbol, signed_qty, entry, mark))
        seen_pos_symbols.add(symbol)

    orders_native = tuple(getattr(reconciled, "orders", ()) or ())
    order_coid_by_id: dict[str, str] = {}
    for item in orders_native:
        oid = str(getattr(item, "order_id", "") or "")
        coid = getattr(item, "client_order_id", None)
        if oid and isinstance(coid, str) and coid.strip():
            order_coid_by_id[oid] = coid.strip()

    fills: list[Execution] = []
    exec_abs_sum_by_order: dict[str, Decimal] = {}
    for ex in getattr(reconciled, "executions", ()) or ():
        exec_id = str(getattr(ex, "execution_id", "") or "")
        order_id = str(getattr(ex, "order_id", "") or "")
        symbol = str(getattr(ex, "symbol", "") or "")
        side = getattr(ex, "side", None)
        qty_abs = _finite_decimal(getattr(ex, "quantity", None))
        price = _finite_decimal(getattr(ex, "price", None))
        fee = _finite_decimal(getattr(ex, "fee", None))
        ts_ms = getattr(ex, "timestamp_ms", None)
        if (
            not exec_id
            or not order_id
            or not symbol
            or side not in ("buy", "sell")
            or qty_abs is None
            or qty_abs <= 0
            or price is None
            or price <= 0
            or fee is None
            or type(ts_ms) is not int
        ):
            raise ValueError("BYBIT_EXECUTION_EVIDENCE_INVALID")
        raw_coid = getattr(ex, "client_order_id", None)
        intent_id = (
            raw_coid.strip()
            if isinstance(raw_coid, str) and raw_coid.strip()
            else order_coid_by_id.get(order_id, "")
        )
        signed_qty = qty_abs if side == "buy" else -qty_abs
        fills.append(Execution(exec_id, intent_id, symbol, signed_qty, price, fee, ts_ms))
        exec_abs_sum_by_order[order_id] = exec_abs_sum_by_order.get(order_id, _ZERO) + qty_abs

    history_complete = bool(getattr(reconciled, "history_complete", False))
    orders: list[BrokerOrder] = []
    for item in orders_native:
        oid = str(getattr(item, "order_id", "") or "")
        symbol = str(getattr(item, "symbol", "") or "")
        side = getattr(item, "side", None)
        qty_abs = _finite_decimal(getattr(item, "quantity", None))
        filled_abs = _finite_decimal(getattr(item, "filled_quantity", None))
        raw_status = str(getattr(item, "status", "") or "")
        reduce_only = getattr(item, "reduce_only", False)
        if (
            not oid
            or not symbol
            or side not in ("buy", "sell")
            or qty_abs is None
            or qty_abs <= 0
            or filled_abs is None
            or filled_abs < 0
            or filled_abs > qty_abs
            or type(reduce_only) is not bool
        ):
            raise ValueError("BYBIT_ORDER_EVIDENCE_INVALID")
        sign = Decimal("1") if side == "buy" else Decimal("-1")
        exec_sum = exec_abs_sum_by_order.get(oid, _ZERO)
        executions_reconciled = history_complete and (exec_sum == filled_abs)
        normalized = normalize_broker_status(
            "bybit_demo",
            raw_status,
            quantity=qty_abs * sign,
            cumulative_filled=filled_abs * sign,
            complete_history=history_complete,
            executions_reconciled=executions_reconciled,
        )
        raw_coid = getattr(item, "client_order_id", None)
        intent_id = raw_coid.strip() if isinstance(raw_coid, str) and raw_coid.strip() else ""
        orders.append(
            BrokerOrder(
                intent_id=intent_id,
                broker_id=oid,
                market=symbol,
                quantity=qty_abs * sign,
                filled=filled_abs * sign,
                status=normalized.status,
                reduce_only=reduce_only,
                raw_status=normalized.raw_status,
                normalization_schema=normalized.schema,
                normalization_provenance=normalized.provenance,
            )
        )

    observed_at_ms = int(getattr(reconciled, "observed_at_ms", 0))
    received_at_ms = int(getattr(reconciled, "received_at_ms", 0))
    server_time_ms = getattr(reconciled, "server_time_ms", None)
    epoch = int(getattr(reconciled, "epoch", 0))
    connected = bool(getattr(reconciled, "connected", False))
    cutoff = getattr(reconciled, "cutoff", None)

    return BrokerSnapshot(
        account_binding=binding,
        endpoint=endpoint,
        observed_at_ms=observed_at_ms,
        received_at_ms=received_at_ms,
        epoch=epoch,
        cash=cash,
        equity=equity,
        unrealized=unrealized,
        initial_margin=initial_margin,
        maintenance_margin=maintenance_margin,
        positions=tuple(positions),
        orders=tuple(orders),
        fills=tuple(fills),
        cutoff=cutoff if isinstance(cutoff, str) and cutoff else None,
        history_complete=history_complete,
        connected=connected,
        financial_complete=financial_complete,
        market_marks=tuple(marks),
        provider_balance=cash,
        provider_buying_power=buying_power,
        provider_financial_provenance=(
            ("equity", "BYBIT_UNIFIED_TOTAL_EQUITY"),
            ("cash", "BYBIT_UNIFIED_TOTAL_WALLET_BALANCE"),
            ("unrealized_pnl", "BYBIT_UNIFIED_TOTAL_PERP_UPL"),
            ("initial_margin", "BYBIT_UNIFIED_TOTAL_INITIAL_MARGIN"),
            ("maintenance_margin", "BYBIT_UNIFIED_TOTAL_MAINTENANCE_MARGIN"),
            ("buying_power", "BYBIT_UNIFIED_TOTAL_AVAILABLE_BALANCE"),
        ),
        provider_time=str(server_time_ms) if isinstance(server_time_ms, int) else None,
        native_account_id=None,
        native_account_source="bybit_v5_unified_ccxt",
        evidence_schema="bybit_demo_ccxt_snapshot_v1",
        uncertainty_reasons=tuple(dict.fromkeys(reasons)),
        native_inventory_read_complete=history_complete,
        native_inventory_count=(len(orders), len(positions), len(fills)),
    )


class BybitDemoGateway:
    """Synchronous BrokerGateway backed by a CCXT BybitDemoReconciler on a dedicated loop."""

    synthetic = False
    endpoint = BYBIT_DEMO_ENDPOINT

    def __init__(
        self,
        reconciler: Any,
        *,
        expected_demo_uid: str,
        loop: asyncio.AbstractEventLoop | None = None,
        command_timeout_s: float = 10.0,
    ) -> None:
        client = getattr(reconciler, "client", None)
        config = getattr(client, "config", None)
        env_val = getattr(getattr(config, "environment", None), "value", None)
        cfg_uid = getattr(config, "expected_demo_uid", None)
        uid = expected_demo_uid.strip()
        if not uid or env_val != "demo" or cfg_uid != uid:
            raise PermissionError("BYBIT_DEMO_SCOPE_UNVERIFIED")

        self._reconciler = reconciler
        self._expected_demo_uid = uid
        self.account_binding = bybit_demo_account_binding(uid)
        self._command_timeout_s = command_timeout_s
        self._external_loop = loop
        self._owned_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._submitted_lock = threading.Lock()
        self._submitted_targets: dict[str, tuple[str, str]] = {}

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._external_loop is not None:
            return self._external_loop
        if self._owned_loop is not None and not self._owned_loop.is_closed():
            return self._owned_loop
        ready = threading.Event()
        holder: dict[str, asyncio.AbstractEventLoop] = {}

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder["loop"] = loop
            ready.set()
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=_runner, name="bybit-demo-ccxt-loop", daemon=True)
        thread.start()
        ready.wait(timeout=5.0)
        loop = holder.get("loop")
        if loop is None:
            raise RuntimeError("BYBIT_EVENT_LOOP_START_FAILED")
        self._owned_loop = loop
        self._thread = thread
        return loop

    def _run_coro(self, coro: Coroutine[Any, Any, _T]) -> _T:
        loop = self._ensure_loop()
        future: concurrent.futures.Future[_T] = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=self._command_timeout_s)

    def start(self) -> None:
        """Catch up via REST and launch background ccxt.pro WS streams."""
        if self._started:
            return
        self._run_coro(self._reconciler.start())
        self._started = True

    def close(self) -> None:
        """Stop WS streams and tear down owned event loop thread."""
        if self._started and (self._external_loop is not None or self._owned_loop is not None):
            try:
                self._run_coro(self._reconciler.stop())
            finally:
                self._started = False
        if self._owned_loop is not None and not self._owned_loop.is_closed():
            self._owned_loop.call_soon_threadsafe(self._owned_loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._owned_loop = None

    def __enter__(self) -> BybitDemoGateway:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def observe(self, now_ms: int) -> BrokerSnapshot:
        """Read the latest atomic reconciled snapshot without touching network."""
        snap = self._reconciler.snapshot()
        return snapshot_from_bybit_reconciliation(
            snap, expected_demo_uid=self._expected_demo_uid
        )

    def _require_execution_mode(self) -> None:
        mode = getattr(getattr(self._reconciler.client.config, "mode", None), "value", None)
        if mode != "execution":
            raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")

    def submit(self, intent: dict[str, Any]) -> Outcome:
        """Submit one signed order intent with orderLinkId==intent_id; never retry."""
        self._require_execution_mode()
        intent_id = str(intent.get("intent_id", "") or "").strip()
        symbol = str(intent.get("market", "") or "").strip()
        qty = _finite_decimal(intent.get("quantity"))
        reduce_only = intent.get("reduce_only", False)
        if (
            not intent_id
            or len(intent_id) > 36
            or not symbol
            or qty is None
            or qty == 0
            or type(reduce_only) is not bool
        ):
            raise ValueError("INVALID_ORDER_INTENT")

        side = "buy" if qty > 0 else "sell"
        raw_price = intent.get("price") if intent.get("price") is not None else intent.get("limit_price")
        price_dec = _finite_decimal(raw_price) if raw_price is not None else None
        order_type = str(intent.get("order_type") or ("limit" if price_dec is not None else "market")).lower()
        if order_type == "limit" and (price_dec is None or price_dec <= 0):
            raise ValueError("INVALID_LIMIT_PRICE")

        try:
            order = self._run_coro(
                self._reconciler.client.create_order(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    amount=float(abs(qty)),
                    price=float(price_dec) if price_dec is not None else None,
                    reduce_only=reduce_only,
                    client_order_id=intent_id,
                )
            )
        except Exception as exc:
            exc_name = type(exc).__name__
            if exc_name in ("BybitInvalidOrderError", "BybitInsufficientFundsError"):
                return Outcome(status="REJECTED", broker_id=None, reason=exc_name)
            return Outcome(status="UNKNOWN", broker_id=None, reason=f"SUBMIT_AMBIGUOUS:{exc_name}")

        broker_id = str(getattr(order, "order_id", "") or "").strip()
        if not broker_id:
            return Outcome(status="UNKNOWN", broker_id=None, reason="MISSING_BROKER_ID")

        with self._submitted_lock:
            self._submitted_targets[intent_id] = (broker_id, symbol)

        raw_status = str(getattr(order, "status", "") or "").strip().lower()
        status_map = {
            "open": "ACCEPTED",
            "new": "ACCEPTED",
            "accepted": "ACCEPTED",
            "partially_filled": "PARTIAL",
            "filled": "FILLED",
            "closed": "FILLED",
            "rejected": "REJECTED",
        }
        mapped = status_map.get(raw_status, "UNKNOWN")
        return Outcome(status=mapped, broker_id=broker_id)

    def cancel(self, intent_id: str) -> Outcome:
        """Cancel an active order identified by intent_id without blind retries."""
        self._require_execution_mode()
        key = str(intent_id or "").strip()
        if not key:
            return Outcome(status="UNKNOWN", reason="INVALID_INTENT_ID")

        target: tuple[str, str] | None = None
        snap = self._reconciler.snapshot()
        for item in getattr(snap, "orders", ()) or ():
            if getattr(item, "client_order_id", None) == key and getattr(item, "order_id", None):
                target = (str(item.order_id), str(item.symbol))
                break
        if target is None:
            with self._submitted_lock:
                target = self._submitted_targets.get(key)
        if target is None:
            return Outcome(status="UNKNOWN", reason="CANCEL_TARGET_UNKNOWN")

        order_id, symbol = target
        try:
            cancelled = self._run_coro(
                self._reconciler.client.cancel_order(order_id, symbol=symbol)
            )
        except Exception as exc:
            return Outcome(
                status="UNKNOWN",
                broker_id=order_id,
                reason=f"CANCEL_AMBIGUOUS:{type(exc).__name__}",
            )
        if cancelled:
            return Outcome(status="CANCELLED", broker_id=order_id)
        return Outcome(status="UNKNOWN", broker_id=order_id, reason="ORDER_NOT_FOUND")
