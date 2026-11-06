"""Independent Bybit Demo account observer; authenticated read-only evidence only.

Uses the pinned trading-connector's V5 Demo identity and complete supported
position/order readers. No provider mutation method is imported or invoked.
This observer does NOT drive the synthetic Guardian or establish trade readiness.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evidence import canonical
from .gateway import BrokerSnapshot, inspect_bybit_demo_readonly

POLL_SECONDS = 45
running = True


def _stop(*_: Any) -> None:
    global running
    running = False


def config_from_environment() -> Any:
    """Reject generic/mainnet/testnet credentials and writable connector mode."""
    from connectors.bybit.config import BybitConfig, Environment, Mode

    if (os.environ.get("BYBIT_ENVIRONMENT") != "demo"
            or os.environ.get("BYBIT_MODE") != "read_only"
            or not os.environ.get("BYBIT_DEMO_API_KEY")
            or not os.environ.get("BYBIT_DEMO_API_SECRET")
            or not os.environ.get("BYBIT_DEMO_EXPECTED_UID")):
        raise PermissionError("DEDICATED_READ_ONLY_BYBIT_DEMO_CREDENTIALS_REQUIRED")
    return BybitConfig(
        environment=Environment.DEMO,
        mode=Mode.READ_ONLY,
        api_key=os.environ["BYBIT_DEMO_API_KEY"],
        api_secret=os.environ["BYBIT_DEMO_API_SECRET"],
        expected_demo_uid=os.environ["BYBIT_DEMO_EXPECTED_UID"],
    )


def observation_record(snapshot: BrokerSnapshot, *, key_is_read_only: bool) -> dict[str, Any]:
    """Keep native quantities/provenance without ever exporting UID/key/raw payload."""
    return {
        "schema": "bybit_demo_observation_v1",
        "account_source": "bybit_demo",
        "environment": "demo",
        "account_binding_sha": snapshot.account_binding,
        "observed_at_ms": snapshot.received_at_ms,
        "identity_verified": True,
        "api_key_read_only": key_is_read_only,
        "read_only_mode": True,
        "healthy": True,  # successful bounded REST observation, not financial readiness
        "equity_usd": str(snapshot.equity),
        "equity_provenance": "PROVIDER_TOTAL_EQUITY_USD",
        "available_usd": str(snapshot.provider_buying_power),
        "available_provenance": "PROVIDER_TOTAL_AVAILABLE_BALANCE_USD",
        "ledger_cash": "UNKNOWN",
        "account_margin": "UNKNOWN",
        "positions_count": len(snapshot.positions),
        "orders_count": len(snapshot.orders),
        "positions": [
            {"market": p.market, "signed_quantity": str(p.quantity),
             "entry_price": str(p.entry_price), "mark_price": str(p.mark_price)}
            for p in snapshot.positions
        ],
        "open_orders": [
            {"order_id_sha": hashlib.sha256(o.broker_id.encode()).hexdigest(),
             "market": o.market, "signed_quantity": str(o.quantity),
             "signed_filled": str(o.filled), "status": o.status}
            for o in snapshot.orders
        ],
        "history_complete": False,
        "cutoff": None,
        "private_stream_connected": False,
        "opening_ready": False,
        "guardian_order_route": "NONE",
        "reconciliation": "RECONCILIATION_UNCERTAIN",
        "uncertainty_reasons": list(snapshot.uncertainty_reasons),
        "mutations": 0,
    }


def write_observation(state_dir: Path, record: dict[str, Any]) -> None:
    """Private append-only account tape and role-owned atomic heartbeat."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tape = state_dir / "observations.jsonl"
    with tape.open("ab") as sink:
        sink.write(canonical(record) + b"\n")
    os.chmod(tape, 0o600)
    heartbeat_dir = state_dir.parent / "health" / "bybit"
    heartbeat_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = heartbeat_dir / "heartbeat.json"
    temporary = heartbeat_dir / "heartbeat.tmp"
    temporary.write_bytes(canonical({
        "schema": "bybit_demo_heartbeat_v1",
        "heartbeat_ms": record["observed_at_ms"],
        "healthy": record.get("healthy") is True,
        "identity_verified": record.get("identity_verified") is True,
        "history_complete": False,
        "opening_ready": False,
        "reconciliation": "RECONCILIATION_UNCERTAIN",
        "mutations": 0,
    }))
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)


async def observe_loop(
    state_dir: Path, *, poll_seconds: int = POLL_SECONDS,
    client_factory: Callable[[Any], Any] | None = None,
    max_iterations: int | None = None,
) -> None:
    if not 10 <= poll_seconds <= 120:
        raise ValueError("BYBIT_POLL_CADENCE_INVALID")
    config = config_from_environment()
    if client_factory is None:
        from connectors.bybit.client import BybitClient

        client_factory = BybitClient
    client: Any = None
    iterations = 0
    try:
        while running and (max_iterations is None or iterations < max_iterations):
            now_ms = time.time_ns() // 1_000_000
            try:
                if client is None:
                    client = client_factory(config)
                    await client.connect()  # authenticated dedicated demo UID & key
                snapshot = await inspect_bybit_demo_readonly(client, now_ms)
                record = observation_record(
                    snapshot,
                    key_is_read_only=bool(
                        client.demo_identity_evidence.get("key_is_read_only") is True
                    ),
                )
            except Exception as error:
                # No raw exception text: provider/transport errors can contain
                # sensitive account payloads. Reset the session on any failure.
                record = {
                    "schema": "bybit_demo_observation_v1", "observed_at_ms": now_ms,
                    "healthy": False, "identity_verified": False,
                    "error_type": type(error).__name__,
                    "reconciliation": "RECONCILIATION_UNCERTAIN",
                    "history_complete": False, "opening_ready": False,
                    "guardian_order_route": "NONE", "mutations": 0,
                }
                if client is not None:
                    await client.close()
                    client = None
            write_observation(state_dir, record)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            for _ in range(poll_seconds * 10):
                if not running:
                    return
                await asyncio.sleep(0.1)
    finally:
        if client is not None:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Demo read-only account observer")
    parser.add_argument("--once", action="store_true", help="One authenticated read-only observation")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    state = Path(os.environ.get("PROP_BYBIT_STATE", "/var/lib/prop-production/bybit"))
    asyncio.run(observe_loop(state, max_iterations=1 if args.once else None))


if __name__ == "__main__":
    main()
