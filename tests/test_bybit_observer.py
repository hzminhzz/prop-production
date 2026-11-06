"""Dedicated Bybit Demo observer lifecycle, protected evidence and fail-closed status."""
import asyncio
import json
import stat
from decimal import Decimal
from types import SimpleNamespace

import pytest

from prop_production import bybit_observer


class Client:
    def __init__(self, config, *, fail=False):
        self.config = config
        self.fail = fail
        self.connected = False
        self.closed = False
        self.demo_identity_verified = False
        self.demo_identity_evidence = {}
        self.calls = []

    async def connect(self):
        self.connected = True
        self.demo_identity_verified = True
        self.demo_identity_evidence = {
            "environment": "demo", "private_rest": "api-demo.bybit.com",
            "authenticated_uid_matches_expected": True,
            "dedicated_demo_key_matches": True,
            "unified_account_verified": True,
            "key_is_read_only": True,
        }

    async def close(self):
        self.closed = True

    async def fetch_demo_account_summary(self):
        self.calls.append("wallet")
        if self.fail:
            raise TimeoutError("fixture-secret-must-not-leak")
        return SimpleNamespace(equity_usd=Decimal("25000.001"),
                               available_usd=Decimal("22000.01"))

    async def fetch_demo_positions(self):
        self.calls.append("positions")
        return []

    async def fetch_demo_open_orders(self):
        self.calls.append("orders")
        return []


def config():
    return SimpleNamespace(environment=SimpleNamespace(value="demo"),
                           mode=SimpleNamespace(value="read_only"),
                           expected_demo_uid="secret-uid-87654")


def test_observer_writes_redacted_demo_evidence_and_closes(monkeypatch, tmp_path):
    instances = []
    monkeypatch.setattr(bybit_observer, "config_from_environment", config)
    monkeypatch.setattr(bybit_observer, "running", True)

    def factory(c):
        client = Client(c)
        instances.append(client)
        return client

    asyncio.run(bybit_observer.observe_loop(tmp_path / "bybit", client_factory=factory,
                                            max_iterations=1))
    record = json.loads((tmp_path / "bybit" / "observations.jsonl").read_text().strip())
    heartbeat = json.loads((tmp_path / "health" / "bybit" / "heartbeat.json").read_text())
    assert record["identity_verified"] is True
    assert record["environment"] == "demo"
    assert record["equity_usd"] == "25000.001"
    assert record["available_usd"] == "22000.01"
    assert record["ledger_cash"] == "UNKNOWN"
    assert record["positions_count"] == 0
    assert record["orders_count"] == 0
    assert record["history_complete"] is False
    assert record["opening_ready"] is False
    assert record["mutations"] == 0
    assert "secret-uid" not in json.dumps(record)
    assert heartbeat["healthy"] is True
    assert heartbeat["opening_ready"] is False
    assert instances[0].calls == ["wallet", "positions", "orders"]
    assert instances[0].closed is True
    assert stat.S_IMODE((tmp_path / "bybit" / "observations.jsonl").stat().st_mode) == 0o600


def test_observer_failure_never_writes_raw_exception_or_fabricates_flatness(monkeypatch, tmp_path):
    instances = []
    monkeypatch.setattr(bybit_observer, "config_from_environment", config)
    monkeypatch.setattr(bybit_observer, "running", True)

    def factory(c):
        client = Client(c, fail=True)
        instances.append(client)
        return client

    asyncio.run(bybit_observer.observe_loop(tmp_path / "bybit", client_factory=factory,
                                            max_iterations=1))
    record = json.loads((tmp_path / "bybit" / "observations.jsonl").read_text().strip())
    assert record["healthy"] is False
    assert record["identity_verified"] is False
    assert record["opening_ready"] is False
    assert "positions_count" not in record
    assert "equity_usd" not in record
    assert "secret-must-not-leak" not in str(record)
    assert instances[0].closed is True


def test_watchdog_reports_bybit_read_only_separately_from_market_data(tmp_path):
    from prop_production.watchdog import assess, write_evidence

    bybit_observer.write_observation(tmp_path / "bybit", {
        "schema": "bybit_demo_observation_v1", "observed_at_ms": 9000,
        "healthy": True, "identity_verified": True,
    })
    record = assess(tmp_path, now_ms=10_000)
    assert record["bybit_status"] == "OBSERVED_READ_ONLY"
    assert record["market_status"] == "UNKNOWN"
    assert record["status"] == "OPERATIONAL_BUT_BLOCKED"
    write_evidence(tmp_path / "watchdog", record)

    stale = assess(tmp_path, now_ms=200_000)
    assert stale["bybit_status"] == "UNKNOWN_OR_STALE"
    assert "BYBIT_DEMO_OBSERVER_STALE_OR_UNKNOWN" in stale["reasons"]


def test_bad_poll_cadence_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(bybit_observer, "config_from_environment", config)
    with pytest.raises(ValueError, match="BYBIT_POLL_CADENCE_INVALID"):
        asyncio.run(bybit_observer.observe_loop(tmp_path / "bybit",
                                                client_factory=Client, poll_seconds=1,
                                                max_iterations=1))

