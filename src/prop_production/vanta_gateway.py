"""Vanta account-bound read + opt-in single-order BrokerGateway implementation."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .evidence import decimal, digest
from .gateway import (
    AsyncLoopRunner,
    BrokerOrder,
    BrokerSnapshot,
    MarketSpec,
    Outcome,
    Position,
    normalize_broker_status,
)

VANTA_API_URL = "https://app.vantatrading.io"
_ZERO = Decimal("0")
_VANTA_DEFAULT_MIN_AMOUNT = Decimal("1e-8")
_CONSERVATION_TOLERANCE = Decimal("0.01")
_TRANSIENT_READ_ERRORS = frozenset(
    {
        "VantaTransportError",
        "VantaServerError",
        "VantaRateLimitError",
        "VantaResponseError",
    }
)
_FATAL_READ_ERRORS = frozenset(
    {
        "VantaAccountScopeError",
        "VantaAuthenticationError",
        "VantaPermissionError",
        "VantaNotFoundError",
    }
)
_REJECTED_SUBMIT_ERRORS = frozenset(
    {
        "VantaValidationError",
        "VantaAuthenticationError",
        "VantaPermissionError",
        "VantaNotFoundError",
        "VantaConflictError",
        "VantaRateLimitError",
    }
)
_REJECTED_STATUS_CODES = frozenset({400, 401, 403, 404, 409, 429})


def vanta_account_binding(account_id: str, endpoint: str = VANTA_API_URL) -> str:
    """Return canonical Guardian account_binding digest for a Vanta account."""
    clean_id = account_id.strip()
    clean_ep = endpoint.rstrip("/")
    if not clean_id or not clean_ep:
        raise ValueError("VANTA_ACCOUNT_AND_ENDPOINT_REQUIRED")
    return digest({"provider": "vanta", "endpoint": clean_ep, "account": clean_id})


def _section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    val = payload.get(key)
    return val if isinstance(val, Mapping) else {}


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = decimal(value)
    except Exception:
        return None
    return dec if dec.is_finite() else None


def _parse_iso_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


def is_transient_vanta_read_error(exc: BaseException) -> bool:
    """Return True when a Vanta read failure is transient and should degrade to BLOCKED."""
    name = type(exc).__name__
    if isinstance(exc, ValueError) or name in _FATAL_READ_ERRORS:
        return False
    if name in _TRANSIENT_READ_ERRORS or isinstance(exc, TimeoutError):
        return True
    status_code = _status_code(exc)
    return status_code is not None and (status_code == 429 or status_code >= 500)


def _is_not_found_error(exc: BaseException) -> bool:
    return type(exc).__name__ == "VantaNotFoundError" or _status_code(exc) == 404


def _is_definitive_rejection(exc: BaseException) -> bool:
    if type(exc).__name__ in _REJECTED_SUBMIT_ERRORS:
        return True
    status_code = _status_code(exc)
    return status_code is not None and status_code in _REJECTED_STATUS_CODES


def _is_rate_limit_error(exc: BaseException) -> bool:
    return type(exc).__name__ == "VantaRateLimitError" or _status_code(exc) == 429


def _parse_retry_after_s(exc: BaseException) -> int:
    raw = getattr(exc, "retry_after", None)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    if getattr(exc, "code", None) == "too_many_failed_requests":
        return 900
    return 60


def _rejection_reason(exc: BaseException) -> str:
    status_code = _status_code(exc)
    code = getattr(exc, "code", None)
    prefix = f"HTTP_{status_code}" if status_code is not None else type(exc).__name__
    if isinstance(code, str) and code.strip():
        return f"{prefix}:{code.strip()}"
    return f"{prefix}:{type(exc).__name__}" if status_code is not None else prefix


def build_vanta_markets(trade_pairs: Mapping[str, Any]) -> dict[str, MarketSpec]:
    """Build coin -> MarketSpec from GET /api/v1/trading/trade-pairs payload."""
    out: dict[str, MarketSpec] = {}
    deferred_pairs: list[tuple[str, MarketSpec]] = []
    for row in trade_pairs.get("allowed") or ():
        if not isinstance(row, Mapping):
            continue
        pair_id = str(row.get("tradePairId") or "").strip()
        if not pair_id:
            continue
        spec = MarketSpec(symbol=pair_id, min_amount=_VANTA_DEFAULT_MIN_AMOUNT, amount_step=_ZERO)
        source = str(row.get("source") or "").strip().lower()
        hl_coin = str(row.get("hlCoin") or "").strip()
        if source in ("", "hyperliquid"):
            if hl_coin:
                out.setdefault(hl_coin, spec)
            out.setdefault(pair_id, spec)
        else:
            deferred_pairs.append((pair_id, spec))
    for pair_id, spec in deferred_pairs:
        out.setdefault(pair_id, spec)
    return out


class VantaGateway:
    """Synchronous BrokerGateway adapter over async VantaClient + opt-in VantaOrderClient."""

    synthetic = False

    def __init__(
        self,
        client: Any,
        *,
        account_id: str,
        endpoint: str = VANTA_API_URL,
        order_client: Any | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        command_timeout_s: float = 15.0,
        expected_account_size: Decimal | None = None,
        broker_id_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        clean_id = account_id.strip()
        clean_ep = endpoint.rstrip("/")
        if not clean_id:
            raise ValueError("VANTA_ACCOUNT_ID_REQUIRED")
        if expected_account_size is not None and (
            not expected_account_size.is_finite() or expected_account_size <= _ZERO
        ):
            raise ValueError("INVALID_EXPECTED_ACCOUNT_SIZE")
        self._client = client
        self._account_id = clean_id
        self.endpoint = clean_ep
        self._binding = vanta_account_binding(clean_id, clean_ep)
        self._order_client = order_client
        self._loop_runner = AsyncLoopRunner(
            name="vanta-gateway-loop", loop=loop, timeout_s=command_timeout_s
        )
        self._expected_account_size = expected_account_size
        self._broker_id_lookup = broker_id_lookup
        self._submitted_lock = threading.Lock()
        self._submitted_targets: dict[str, str] = {}
        self._broker_to_intent: dict[str, str] = {}
        self._last_positions: dict[str, Decimal] = {}
        self._paused_until_ms: int = 0

    def start(self) -> None:
        self._loop_runner.ensure_loop()

    def close(self) -> None:
        self._loop_runner.close(self._client, self._order_client)

    def __enter__(self) -> VantaGateway:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def load_markets(self) -> dict[str, MarketSpec]:
        """Fetch tradable Vanta trade-pairs via read-only GET endpoint."""
        payload = self._loop_runner.run(self._client.get_trade_pairs())
        return build_vanta_markets(payload)

    def observe(self, now_ms: int) -> BrokerSnapshot:
        """Fetch account, positions, and pending orders via read-only Vanta REST."""

        async def _fetch() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
            return (
                await self._client.get_account(),
                await self._client.get_positions(),
                await self._client.get_orders(),
            )

        try:
            acct, raw_positions, raw_orders = self._loop_runner.run(_fetch())
        except Exception as exc:
            if not is_transient_vanta_read_error(exc):
                raise
            return BrokerSnapshot(
                account_binding=self._binding,
                endpoint=self.endpoint,
                observed_at_ms=now_ms,
                received_at_ms=now_ms,
                epoch=0,
                cash=None,
                equity=None,
                unrealized=None,
                initial_margin=None,
                maintenance_margin=None,
                financial_complete=False,
                native_account_id=self._account_id,
                native_account_source="vanta",
                evidence_schema="vanta_rest_v1",
                uncertainty_reasons=(
                    "PROVIDER_CUTOFF_UNPROVEN",
                    "STREAM_NOT_CONNECTED",
                    f"VANTA_READ_UNAVAILABLE:{type(exc).__name__}",
                ),
                native_inventory_read_complete=False,
            )

        if str(acct.get("accountId") or "").strip() != self._account_id:
            raise ValueError("VANTA_ACCOUNT_SCOPE_MISMATCH")

        reasons: list[str] = ["PROVIDER_CUTOFF_UNPROVEN", "STREAM_NOT_CONNECTED"]
        if self._expected_account_size is not None and acct.get("accountSize") is not None:
            if _finite_decimal(acct.get("accountSize")) != self._expected_account_size:
                reasons.append("VANTA_ACCOUNT_SIZE_MISMATCH")

        account_section = _section(acct, "account")
        eval_section = _section(acct, "evaluation")
        meta_section = _section(acct, "meta")

        cash = _finite_decimal(account_section.get("currentBalance"))
        equity = _finite_decimal(account_section.get("currentEquity"))
        unrealized = _finite_decimal(account_section.get("openPnL"))
        initial_margin = _finite_decimal(account_section.get("capitalUsed")) or _ZERO

        if eval_section.get("isEliminated") is True or isinstance(acct.get("elimination"), Mapping):
            reasons.append("VANTA_ACCOUNT_ELIMINATED")
        if meta_section.get("stale") is True:
            reasons.append("VANTA_ACCOUNT_META_STALE")
        if cash is None or equity is None or unrealized is None:
            reasons.append("VANTA_FINANCE_INCOMPLETE")
        elif abs(equity - (cash + unrealized)) > _CONSERVATION_TOLERANCE:
            reasons.append("VANTA_FINANCE_CONSERVATION_MISMATCH")

        identity = getattr(self._client, "_identity", None)
        if isinstance(identity, Mapping):
            paused_ms = _parse_iso_ms(identity.get("orderPlacementPausedUntil"))
            if paused_ms is not None and paused_ms > now_ms:
                with self._submitted_lock:
                    self._paused_until_ms = max(self._paused_until_ms, paused_ms)

        positions: list[Position] = []
        pos_by_pair: dict[str, Decimal] = {}
        for row in raw_positions:
            if not isinstance(row, Mapping):
                reasons.append("VANTA_POSITION_INVALID")
                continue
            pair = str(row.get("tradePair") or "").strip()
            ptype = str(row.get("positionType") or "").strip().upper()
            net_qty = _finite_decimal(row.get("netQuantity"))
            entry = _finite_decimal(row.get("averageEntryPrice"))
            net_val = _finite_decimal(row.get("netValue"))
            if (
                not pair
                or ptype not in ("LONG", "SHORT")
                or net_qty is None
                or entry is None
                or entry < _ZERO
            ):
                reasons.append("VANTA_POSITION_INVALID")
                continue
            if net_qty == _ZERO:
                continue
            if (ptype == "LONG" and net_qty <= _ZERO) or (ptype == "SHORT" and net_qty >= _ZERO):
                reasons.append("VANTA_POSITION_INVALID")
                continue
            mark = abs(net_val / abs(net_qty)) if net_val is not None else entry
            if mark <= _ZERO:
                mark = entry
            if mark <= _ZERO:
                reasons.append("VANTA_POSITION_INVALID")
                continue
            positions.append(Position(market=pair, quantity=net_qty, entry_price=entry, mark_price=mark))
            pos_by_pair[pair] = pos_by_pair.get(pair, _ZERO) + net_qty

        with self._submitted_lock:
            self._last_positions = pos_by_pair
            broker_to_intent = dict(self._broker_to_intent)

        orders: list[BrokerOrder] = []
        for row in raw_orders:
            if not isinstance(row, Mapping):
                continue
            uuid = str(row.get("orderUuid") or "").strip()
            pair = str(row.get("tradePair") or "").strip()
            if not uuid or not pair:
                continue
            otype = str(row.get("orderType") or "").strip().upper()
            exec_type = str(row.get("executionType") or row.get("execution_type") or "").strip().upper()
            raw_qty = _finite_decimal(row.get("quantity"))
            if raw_qty is not None and raw_qty != _ZERO:
                qty = abs(raw_qty)
            else:
                val = _finite_decimal(row.get("value"))
                limit_px = _finite_decimal(
                    row.get("limitPrice") if row.get("limitPrice") is not None else row.get("limit_price")
                )
                if val is not None and val > _ZERO and limit_px is not None and limit_px > _ZERO:
                    qty = val / limit_px
                else:
                    qty = Decimal("1")
            signed_qty = -qty if otype == "SHORT" else qty
            reduce_only = otype == "FLAT" or exec_type == "BRACKET"
            norm = normalize_broker_status(
                "vanta",
                "pending",
                quantity=signed_qty,
                cumulative_filled=_ZERO,
                complete_history=False,
                executions_reconciled=False,
            )
            orders.append(
                BrokerOrder(
                    intent_id=broker_to_intent.get(uuid, ""),
                    broker_id=uuid,
                    market=pair,
                    quantity=signed_qty,
                    filled=_ZERO,
                    status=norm.status,
                    reduce_only=reduce_only,
                    raw_status=norm.raw_status,
                    normalization_schema=norm.schema,
                    normalization_provenance=norm.provenance,
                )
            )

        generated_ms = meta_section.get("generatedAtMs")
        return BrokerSnapshot(
            account_binding=self._binding,
            endpoint=self.endpoint,
            observed_at_ms=now_ms,
            received_at_ms=now_ms,
            epoch=0,
            cash=cash,
            equity=equity,
            unrealized=unrealized,
            initial_margin=initial_margin,
            maintenance_margin=_ZERO,
            positions=tuple(positions),
            orders=tuple(orders),
            fills=(),
            cutoff=None,
            history_complete=False,
            connected=False,
            financial_complete=(cash is not None and equity is not None and unrealized is not None),
            market_marks=tuple((p.market, p.mark_price, now_ms) for p in positions),
            provider_balance=cash,
            provider_time=str(generated_ms) if generated_ms is not None else None,
            native_account_id=self._account_id,
            native_account_source="vanta",
            evidence_schema="vanta_rest_v1",
            uncertainty_reasons=tuple(sorted(set(reasons))),
            native_inventory_read_complete=True,
            native_inventory_count=(len(orders), len(positions), 0),
        )

    def _require_order_client(self) -> Any:
        if self._order_client is None:
            raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")
        return self._order_client

    def _handle_rejection_exception(
        self, exc: BaseException, *, broker_id: str | None = None
    ) -> Outcome | None:
        if not _is_definitive_rejection(exc):
            return None
        if _is_rate_limit_error(exc):
            retry_s = _parse_retry_after_s(exc)
            now_ms = int(time.time() * 1000)
            with self._submitted_lock:
                self._paused_until_ms = max(self._paused_until_ms, now_ms + retry_s * 1000)
        return Outcome(status="REJECTED", broker_id=broker_id, reason=_rejection_reason(exc))

    def submit(self, intent: dict[str, Any]) -> Outcome:
        """Submit one order to Vanta; never retries ambiguous errors (no idempotency key)."""
        order_client = self._require_order_client()
        intent_id = str(intent.get("intent_id", "") or "").strip()
        trade_pair = str(intent.get("market", "") or "").strip()
        qty = _finite_decimal(intent.get("quantity"))
        reduce_only = intent.get("reduce_only", False)
        raw_mode = intent.get("order_type", "market")
        if not isinstance(raw_mode, str):
            raise ValueError("INVALID_ORDER_INTENT")
        order_mode = raw_mode.strip().lower()
        if (
            not intent_id
            or not trade_pair
            or qty is None
            or qty == _ZERO
            or type(reduce_only) is not bool
            or order_mode not in ("market", "limit")
        ):
            raise ValueError("INVALID_ORDER_INTENT")

        limit_price: Decimal | None = None
        if order_mode == "limit":
            limit_price = _finite_decimal(intent.get("limit_price"))
            if limit_price is None or limit_price <= _ZERO:
                raise ValueError("INVALID_ORDER_INTENT")

        now_ms = int(time.time() * 1000)
        with self._submitted_lock:
            paused_until_ms = self._paused_until_ms
            current_pos = self._last_positions.get(trade_pair, _ZERO)
        if now_ms < paused_until_ms:
            return Outcome(status="REJECTED", broker_id=None, reason="VANTA_COOLDOWN_ACTIVE")

        if (
            reduce_only
            and order_mode == "market"
            and current_pos != _ZERO
            and ((current_pos > _ZERO and qty < _ZERO) or (current_pos < _ZERO and qty > _ZERO))
            and abs(qty) >= abs(current_pos)
        ):
            order_type = "FLAT"
            submit_qty = abs(current_pos)
        else:
            order_type = "LONG" if qty > _ZERO else "SHORT"
            submit_qty = abs(qty)

        payload: dict[str, Any] = {
            "execution_type": "LIMIT" if order_mode == "limit" else "MARKET",
            "trade_pair": trade_pair,
            "order_type": order_type,
            "quantity": submit_qty,
        }
        if limit_price is not None:
            payload["limit_price"] = limit_price

        try:
            ack = self._loop_runner.run(order_client.submit_order(payload))
        except Exception as exc:
            rejected = self._handle_rejection_exception(exc)
            if rejected is not None:
                return rejected
            return Outcome(
                status="UNKNOWN",
                broker_id=None,
                reason=f"SUBMIT_AMBIGUOUS:{type(exc).__name__}",
            )

        order = ack.get("order") if isinstance(ack.get("order"), Mapping) else {}
        broker_id = str(order.get("orderUuid") or ack.get("order_uuid") or "").strip()
        if not broker_id:
            return Outcome(status="UNKNOWN", broker_id=None, reason="MISSING_BROKER_ID")

        with self._submitted_lock:
            self._submitted_targets[intent_id] = broker_id
            self._broker_to_intent[broker_id] = intent_id

        if ack.get("success") is False:
            return Outcome(status="REJECTED", broker_id=broker_id, reason=str(ack.get("error_message") or "REJECTED"))
        filled_flag = order.get("filled")
        if filled_flag is True:
            binding_cap = order.get("bindingCap") if order.get("bindingCap") is not None else ack.get("bindingCap")
            reason: str | None = None
            if binding_cap is not None and binding_cap != "" and binding_cap is not False:
                cap_text = (
                    json.dumps(binding_cap, sort_keys=True, default=str)
                    if isinstance(binding_cap, Mapping)
                    else str(binding_cap)
                )
                reason = f"BINDING_CAP:{cap_text}"
            return Outcome(status="FILLED", broker_id=broker_id, reason=reason)
        if filled_flag is False:
            return Outcome(status="ACCEPTED", broker_id=broker_id)
        return Outcome(status="UNKNOWN", broker_id=broker_id, reason="STATUS:MISSING_FILLED_FLAG")

    def cancel(self, intent_id: str) -> Outcome:
        """Cancel a tracked pending order via LIMIT_CANCEL."""
        order_client = self._require_order_client()
        key = str(intent_id or "").strip()
        if not key:
            return Outcome(status="UNKNOWN", reason="INVALID_INTENT_ID")

        with self._submitted_lock:
            broker_id = self._submitted_targets.get(key)
        if not broker_id and self._broker_id_lookup is not None:
            looked_up = self._broker_id_lookup(key)
            broker_id = str(looked_up or "").strip() or None
        if not broker_id:
            return Outcome(status="UNKNOWN", reason="CANCEL_TARGET_UNKNOWN")

        try:
            self._loop_runner.run(
                order_client.submit_order({"execution_type": "LIMIT_CANCEL", "order_uuid": broker_id})
            )
        except Exception as exc:
            if _is_not_found_error(exc):
                return Outcome(status="UNKNOWN", broker_id=broker_id, reason="ORDER_NOT_FOUND")
            rejected = self._handle_rejection_exception(exc, broker_id=broker_id)
            if rejected is not None:
                return rejected
            return Outcome(
                status="UNKNOWN",
                broker_id=broker_id,
                reason=f"CANCEL_AMBIGUOUS:{type(exc).__name__}",
            )
        return Outcome(status="CANCELLED", broker_id=broker_id)
