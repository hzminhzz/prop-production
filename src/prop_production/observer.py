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
    """Normalize one native-evidence result into an evidence record.

    The connector's NativeEvidence carries a NativeFinancial of FinancialField
    objects (value + provenance). Missing evidence stays UNKNOWN; healthy means
    the account is bound with complete financials — bounded uncertainty reasons
    (cash unverified, cutoff unproven) are expected and recorded separately.
    """
    get = result.get if isinstance(result, dict) else (lambda k, d=None: getattr(result, k, d))
    record: dict[str, Any] = {
        "schema": "propr_observation_v1",
        "observed_at_ms": now_ms,
        "account_bound": bool(get("account_bound", False)),
        "account_source": str(get("account_source", "UNKNOWN")),
        "financial_complete": bool(get("financial_complete", False)),
        "inventory_read_complete": bool(get("inventory_read_complete", False)),
        "stream_connected": bool(get("stream_connected", False)),
        "history_complete": bool(get("history_complete", False)),
        "cutoff": str(get("cutoff", "UNKNOWN")),
        "opening_ready": bool(get("opening_ready", False)),
        "reconciliation": "RECONCILIATION_UNCERTAIN",
        # Never derived: cash = equity - unrealized PnL is not verified ledger cash.
        "ledger_cash": "UNKNOWN",
    }
    financial = get("financial")
    if financial is not None:
        fget = financial.get if isinstance(financial, dict) else (lambda k, d=None: getattr(financial, k, d))
        for key in ("balance", "equity", "buying_power", "cash", "unrealized_pnl",
                    "isolated_position_margin", "initial_margin", "maintenance_margin"):
            field = fget(key)
            if field is not None:
                record[key] = str(getattr(field, "value", field))
                record[key + "_provenance"] = str(getattr(field, "provenance", "UNKNOWN"))
    # Healthy: the account is bound with complete financials. The bounded
    # uncertainty reasons are expected in SHADOW and recorded separately.
    record["healthy"] = bool(record["account_bound"] and record["financial_complete"])
    record["uncertainty_reasons"] = tuple(get("reasons", ()) or ())
    return record


async def observe_once() -> dict[str, Any]:
    """One native-evidence observation via the pinned connector's v1 contract."""
    from dataclasses import asdict

    from connectors.propr.account import ReadOnlyAccount, discover_account
    from connectors.propr.config import Environment, ProprConfig
    from connectors.propr.native_evidence import inspect_native_evidence

    endpoint = os.environ.get("PROPR_API_URL", "").rstrip("/") or "https://api.propr.xyz/v1"
    key = os.environ.get("PROPR_LIVE_API_KEY") or os.environ.get("PROPR_API_KEY")
    if not key:
        raise PermissionError("OBSERVER_KEY_MISSING")
    reader = ReadOnlyAccount(ProprConfig(Environment.PRODUCTION, account_id=None,
                                         api_key=key, api_url=endpoint))
    binding = await discover_account(reader.rest)
    native = await inspect_native_evidence(
        reader.rest, account_id=binding.account_id, endpoint=endpoint,
        known_at_ms=int(time.time() * 1000), stream_connected=False)
    return asdict(native)


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
