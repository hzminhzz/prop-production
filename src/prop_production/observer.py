"""Read-only Propr account observation service.

Polls the authenticated read-only account interface and appends durable
observation evidence. Never sends orders and never writes broker state.
Propr available balance is not verified ledger cash: the field is reported
UNKNOWN with provider provenance until the provider's accounting semantics
are authenticated. History completeness is never asserted from matching
REST reads.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any

from .evidence import canonical

POLL_SECONDS = 30

running = True


def _stop(*_: Any) -> None:
    global running
    running = False


def observation_record(result: Any, *, now_ms: int) -> dict[str, Any]:
    """Normalize one ReadOnlyAccount.inspect() result into an evidence record."""
    get = result.get if isinstance(result, dict) else (lambda k, d=None: getattr(result, k, d))
    record: dict[str, Any] = {
        "schema": "propr_observation_v1",
        "observed_at_ms": now_ms,
        "healthy": bool(get("healthy", False)),
        "account_bound": bool(get("account_bound", False)),
        "account_source": str(get("account_source", "UNKNOWN")),
        "financial_fields_verified": bool(get("financial_fields_verified", False)),
        "history_complete": False,
        "reconciliation": "RECONCILIATION_UNCERTAIN",
        # Never derived: cash = equity - unrealized PnL is not verified ledger cash.
        "ledger_cash": "UNKNOWN",
    }
    snapshot = get("snapshot")
    if snapshot is not None:
        fields = snapshot if isinstance(snapshot, dict) else None
        for key in ("equity", "balance", "buying_power", "unrealized_pnl", "margin_used",
                    "initial_margin", "maintenance_margin", "high_water_mark"):
            if isinstance(fields, dict):
                record[key] = str(fields.get(key, "UNKNOWN"))
            else:
                record[key] = str(getattr(snapshot, key, "UNKNOWN"))
    return record


async def observe_once() -> dict[str, Any]:
    from dotenv import load_dotenv

    load_dotenv()
    # Read-only account inspection lives in the pinned connector package, which
    # is deployed next to the application venv, not a build-time dependency.
    from connectors.propr.account import ReadOnlyAccount
    from connectors.propr.config import Environment, ProprConfig

    key = os.environ.get("PROPR_LIVE_API_KEY") or os.environ.get("PROPR_API_KEY")
    if not key:
        raise PermissionError("OBSERVER_KEY_MISSING")
    reader = ReadOnlyAccount(ProprConfig(Environment.PRODUCTION, account_id=None, api_key=key))
    result = await reader.inspect()
    if not isinstance(result, dict):
        raise TypeError("INSPECTION_SCHEMA")
    return result


async def observe_loop(state_dir: Path, *, poll_seconds: int = POLL_SECONDS) -> None:
    """Poll read-only account evidence until stopped. Appends only."""
    state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    evidence_path = state_dir / "observations.jsonl"
    # Observer heartbeat: role-owned dir (prop-observer writes, group reads).
    health = state_dir.parent / "health" / "observer"
    health.mkdir(mode=0o750, parents=True, exist_ok=True)
    heartbeat_path = health / "heartbeat.json"
    while running:
        now_ms = int(time.time() * 1000)
        try:
            record = observation_record(await observe_once(), now_ms=now_ms)
        except Exception as error:  # observation failure is evidence, not a crash
            record = {"schema": "propr_observation_v1", "observed_at_ms": now_ms,
                      "healthy": False, "error": type(error).__name__}
        with open(evidence_path, "ab") as sink:
            sink.write(canonical(record) + b"\n")
        heartbeat_path.write_bytes(canonical({
            "schema": "observer_heartbeat_v1", "heartbeat_ms": now_ms,
            "healthy": bool(record.get("healthy")),
        }))
        os.chmod(heartbeat_path, 0o640)  # watchdog (group prop) must read this evidence
        for _ in range(poll_seconds * 10):
            if not running:
                return
            await asyncio.sleep(0.1)


def main() -> None:
    state = Path(os.environ.get("PROP_OBSERVER_STATE", "/var/lib/prop-production/observer"))
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    asyncio.run(observe_loop(state))


if __name__ == "__main__":
    main()
