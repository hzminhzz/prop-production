"""Vanta account-bound read + opt-in single-order BrokerGateway implementation."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
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


def build_vanta_markets(trade_pairs: Mapping[str, Any]) -> dict[str, MarketSpec]:
    """Build coin -> MarketSpec from GET /api/v1/trading/trade-pairs payload."""
    out: dict[str, MarketSpec] = {}
    for row in trade_pairs.get("allowed") or ():
        if not isinstance(row, Mapping):
            continue
        pair_id = str(row.get("tradePairId") or "").strip()
        if not pair_id:
            continue
        hl_coin = str(row.get("hlCoin") or "").strip()
        if hl_coin:
            out[hl_coin] = MarketSpec(symbol=pair_id, min_amount=_ZERO, amount_step=_ZERO)
        out.setdefault(pair_id, MarketSpec(symbol=pair_id, min_amount=_ZERO, amount_step=_ZERO))
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
    ) -> None:
        clean_id = account_id.strip()
        clean_ep = endpoint.rstrip("/")
        if not clean_id:
            raise ValueError("VANTA_ACCOUNT_ID_REQUIRED")
        self._client = client
        self._account_id = clean_id
        self.endpoint = clean_ep
        self._binding = vanta_account_binding(clean_id, clean_ep)
        self._order_client = order_client
        self._loop_runner = AsyncLoopRunner(
            name="vanta-gateway-loop", loop=loop, timeout_s=command_timeout_s
        )
        self._submitted_lock = threading.Lock()
        self._submitted_targets: dict[str, str] = {}

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

        acct, raw_positions, raw_orders = self._loop_runner.run(_fetch())
        if str(acct.get("accountId") or "").strip() != self._account_id:
            raise ValueError("VANTA_ACCOUNT_SCOPE_MISMATCH")

        reasons: list[str] = ["PROVIDER_CUTOFF_UNPROVEN", "STREAM_NOT_CONNECTED"]
        account_section = _section(acct, "account")
        eval_section = _section(acct, "evaluation")
        meta_section = _section(acct, "meta")

        cash = _finite_decimal(account_section.get("currentBalance"))
        equity = _finite_decimal(account_section.get("currentEquity"))
        unrealized = _finite_decimal(account_section.get("openPnL"))
        initial_margin = _finite_decimal(account_section.get("capitalUsed")) or _ZERO

        if eval_section.get("isEliminated") is True:
            reasons.append("VANTA_ACCOUNT_ELIMINATED")
        if meta_section.get("stale") is True:
            reasons.append("VANTA_ACCOUNT_META_STALE")
        if cash is None or equity is None or unrealized is None:
            reasons.append("VANTA_FINANCE_INCOMPLETE")

        positions: list[Position] = []
        for row in raw_positions:
            pair = str(row.get("tradePair") or "").strip()
            ptype = str(row.get("positionType") or "").strip().upper()
            net_qty = _finite_decimal(row.get("netQuantity"))
            entry = _finite_decimal(row.get("averageEntryPrice"))
            net_val = _finite_decimal(row.get("netValue"))
            if not pair or ptype not in ("LONG", "SHORT") or net_qty is None or net_qty < _ZERO or entry is None:
                reasons.append("VANTA_POSITION_INVALID")
                continue
            if net_qty == _ZERO:
                continue
            signed_qty = net_qty if ptype == "LONG" else -net_qty
            mark = abs(net_val / net_qty) if (net_val is not None and net_qty > _ZERO) else entry
            if mark <= _ZERO:
                mark = entry
            positions.append(Position(market=pair, quantity=signed_qty, entry_price=entry, mark_price=mark))

        orders: list[BrokerOrder] = []
        for row in raw_orders:
            uuid = str(row.get("orderUuid") or "").strip()
            pair = str(row.get("tradePair") or "").strip()
            otype = str(row.get("orderType") or "").strip().upper()
            qty = _finite_decimal(row.get("quantity")) or _finite_decimal(row.get("value")) or Decimal("1")
            signed_qty = -abs(qty) if otype == "SHORT" else abs(qty)
            if not uuid or not pair:
                continue
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
                    intent_id="",
                    broker_id=uuid,
                    market=pair,
                    quantity=signed_qty,
                    filled=_ZERO,
                    status=norm.status,
                    reduce_only=(otype == "FLAT"),
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

    def submit(self, intent: dict[str, Any]) -> Outcome:
        """Submit one MARKET order to Vanta; never retries ambiguous errors (no idempotency key)."""
        order_client = self._require_order_client()
        intent_id = str(intent.get("intent_id", "") or "").strip()
        trade_pair = str(intent.get("market", "") or "").strip()
        qty = _finite_decimal(intent.get("quantity"))
        if not intent_id or not trade_pair or qty is None or qty == _ZERO:
            raise ValueError("INVALID_ORDER_INTENT")

        order_type = "LONG" if qty > _ZERO else "SHORT"
        try:
            ack = self._loop_runner.run(
                order_client.submit_order(
                    {
                        "execution_type": "MARKET",
                        "trade_pair": trade_pair,
                        "order_type": order_type,
                        "quantity": abs(qty),
                    }
                )
            )
        except Exception as exc:
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

        if ack.get("success") is False:
            return Outcome(status="REJECTED", broker_id=broker_id, reason=str(ack.get("error_message") or "REJECTED"))
        filled_flag = order.get("filled")
        if filled_flag is True:
            return Outcome(status="FILLED", broker_id=broker_id)
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
        if not broker_id:
            return Outcome(status="UNKNOWN", reason="CANCEL_TARGET_UNKNOWN")

        try:
            self._loop_runner.run(
                order_client.submit_order({"execution_type": "LIMIT_CANCEL", "order_uuid": broker_id})
            )
        except Exception as exc:
            return Outcome(
                status="UNKNOWN",
                broker_id=broker_id,
                reason=f"CANCEL_AMBIGUOUS:{type(exc).__name__}",
            )
        return Outcome(status="CANCELLED", broker_id=broker_id)
