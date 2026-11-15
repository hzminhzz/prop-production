"""Propr read-only evidence + opt-in single-order BrokerGateway implementation."""

from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal
from typing import Any

from .evidence import decimal, digest
from .gateway import AsyncLoopRunner, BrokerSnapshot, Outcome, inspect_propr_readonly

PROPR_API_URL = "https://api.propr.xyz/v1"
_ZERO = Decimal("0")


def propr_account_binding(account_id: str, endpoint: str = PROPR_API_URL) -> str:
    """Return canonical Guardian account_binding digest matching inspect_propr_readonly."""
    clean_id = account_id.strip()
    clean_ep = endpoint.rstrip("/")
    if not clean_id or not clean_ep:
        raise ValueError("PROPR_ACCOUNT_AND_ENDPOINT_REQUIRED")
    return digest({"provider": "propr", "endpoint": clean_ep, "account": clean_id})


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = decimal(value)
    except Exception:
        return None
    return dec if dec.is_finite() else None


class ProprGateway:
    """Synchronous BrokerGateway adapter over async ProprRest + opt-in ProprOrderClient."""

    synthetic = False

    def __init__(
        self,
        rest: Any,
        *,
        account_id: str,
        endpoint: str = PROPR_API_URL,
        order_client: Any | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        command_timeout_s: float = 15.0,
    ) -> None:
        clean_id = account_id.strip()
        clean_ep = endpoint.rstrip("/")
        if not clean_id:
            raise ValueError("PROPR_ACCOUNT_ID_REQUIRED")
        self._rest = rest
        self._account_id = clean_id
        self.endpoint = clean_ep
        self._binding = propr_account_binding(clean_id, clean_ep)
        self._order_client = order_client
        self._loop_runner = AsyncLoopRunner(
            name="propr-gateway-loop", loop=loop, timeout_s=command_timeout_s
        )
        self._submitted_lock = threading.Lock()
        self._submitted_targets: dict[str, str] = {}

    def start(self) -> None:
        self._loop_runner.ensure_loop()

    def close(self) -> None:
        self._loop_runner.close(self._rest, self._order_client)

    def __enter__(self) -> ProprGateway:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def observe(self, now_ms: int) -> BrokerSnapshot:
        """Read normalized Propr native evidence snapshot via read-only REST."""
        snap = self._loop_runner.run(
            inspect_propr_readonly(self._rest, self._account_id, self.endpoint, now_ms)
        )
        if snap.account_binding != self._binding or snap.endpoint != self.endpoint:
            raise ValueError("PROPR_SCOPE_MISMATCH")
        return snap

    def _require_order_client(self) -> Any:
        if self._order_client is None:
            raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")
        return self._order_client

    def submit(self, intent: dict[str, Any]) -> Outcome:
        """Submit one pre-committed market order intent; never retries ambiguous errors."""
        order_client = self._require_order_client()
        intent_id = str(intent.get("intent_id", "") or "").strip()
        asset = str(intent.get("market", "") or "").strip()
        qty = _finite_decimal(intent.get("quantity"))
        reduce_only = intent.get("reduce_only", False)
        if (
            not intent_id
            or not asset
            or qty is None
            or qty == _ZERO
            or type(reduce_only) is not bool
        ):
            raise ValueError("INVALID_ORDER_INTENT")

        side = "buy" if qty > _ZERO else "sell"
        # Reducing an existing long sells with positionSide=long; reducing a short buys positionSide=short.
        position_side = "long" if (qty > _ZERO) != reduce_only else "short"

        try:
            ack = self._loop_runner.run(
                order_client.submit_market_order(
                    intent_id=intent_id,
                    asset=asset,
                    side=side,
                    position_side=position_side,
                    quantity=abs(qty),
                    reduce_only=reduce_only,
                )
            )
        except Exception as exc:
            return Outcome(
                status="UNKNOWN",
                broker_id=None,
                reason=f"SUBMIT_AMBIGUOUS:{type(exc).__name__}",
            )

        broker_id = str(ack.get("orderId", "") or "").strip()
        if not broker_id:
            return Outcome(status="UNKNOWN", broker_id=None, reason="MISSING_BROKER_ID")

        with self._submitted_lock:
            self._submitted_targets[intent_id] = broker_id

        raw_status = str(ack.get("status", "") or "").strip().lower()
        filled = _finite_decimal(ack.get("cumulativeQuantity")) or _ZERO
        if raw_status == "filled" and filled == abs(qty):
            return Outcome(status="FILLED", broker_id=broker_id)
        if raw_status in ("partially_filled", "partial") and _ZERO < filled < abs(qty):
            return Outcome(status="PARTIAL", broker_id=broker_id)
        if raw_status in ("open", "pending", "accepted"):
            return Outcome(status="ACCEPTED", broker_id=broker_id)
        if raw_status in ("rejected", "expired") and filled == _ZERO:
            return Outcome(status="REJECTED", broker_id=broker_id, reason=raw_status.upper())
        return Outcome(status="UNKNOWN", broker_id=broker_id, reason=f"STATUS:{raw_status or 'MISSING'}")

    def cancel(self, intent_id: str) -> Outcome:
        """Cancel an active order identified by intent_id without blind retries."""
        order_client = self._require_order_client()
        key = str(intent_id or "").strip()
        if not key:
            return Outcome(status="UNKNOWN", reason="INVALID_INTENT_ID")

        with self._submitted_lock:
            broker_id = self._submitted_targets.get(key)
        if not broker_id:
            snap = self.observe(int(time.time() * 1000))
            for item in snap.orders:
                if item.intent_id == key and item.broker_id:
                    broker_id = item.broker_id
                    break
        if not broker_id:
            return Outcome(status="UNKNOWN", reason="CANCEL_TARGET_UNKNOWN")

        try:
            self._loop_runner.run(order_client.cancel_order(broker_id))
        except Exception as exc:
            return Outcome(
                status="UNKNOWN",
                broker_id=broker_id,
                reason=f"CANCEL_AMBIGUOUS:{type(exc).__name__}",
            )
        return Outcome(status="CANCELLED", broker_id=broker_id)
