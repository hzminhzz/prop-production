"""Unit tests for the deployed observer, watchdog and driver modules."""
import json

from prop_production.driver import _heartbeat
from prop_production.observer import observation_record
from prop_production.watchdog import assess


def test_observation_record_flattens_dict_result():
    record = observation_record({"healthy": True, "account_bound": True,
                                 "account_source": "challenge",
                                 "snapshot": {"equity": "4999.225661"}}, now_ms=1000)
    assert record["healthy"] is True
    assert record["account_bound"] is True
    assert record["equity"] == "4999.225661"
    assert record["reconciliation"] == "RECONCILIATION_UNCERTAIN"
    assert record["history_complete"] is False
    assert record["ledger_cash"] == "UNKNOWN"


def test_observation_record_defaults_stay_unknown():
    record = observation_record({}, now_ms=1000)
    assert record["healthy"] is False
    assert record["account_source"] == "UNKNOWN"
    assert "equity" not in record  # no invented financial fields without provider evidence


def test_watchdog_blocks_on_stale_guardian(tmp_path):
    (tmp_path / "health").mkdir()
    reasons = assess(tmp_path, now_ms=10_000_000)["reasons"]
    assert "GUARDIAN_STALE_OR_UNKNOWN" in reasons
    assert "OBSERVER_STALE_OR_UNKNOWN" in reasons


def test_watchdog_blocks_on_unhealthy_observer(tmp_path):
    health = tmp_path / "health"
    (health / "guardian").mkdir(parents=True)
    (health / "observer").mkdir(parents=True)
    (health / "guardian" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000, "halts": []}))
    (health / "observer" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000, "healthy": False}))
    assert "OBSERVER_UNHEALTHY" in assess(tmp_path, now_ms=10_000)["reasons"]


def test_watchdog_healthy_when_fresh(tmp_path):
    health = tmp_path / "health"
    (health / "guardian").mkdir(parents=True)
    (health / "observer").mkdir(parents=True)
    (health / "guardian" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000, "halts": []}))
    (health / "observer" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000, "healthy": True}))
    assessment = assess(tmp_path, now_ms=10_000)
    assert assessment["status"] == "HEALTHY"
    assert assessment["guardian_heartbeat_ms"] == 9_000


def test_watchdog_propagates_latched_halts(tmp_path):
    health = tmp_path / "health"
    (health / "guardian").mkdir(parents=True)
    (health / "observer").mkdir(parents=True)
    (health / "guardian" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000,
                                                      "halts": ["BROKER_UNREACHABLE"]}))
    (health / "observer" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000, "healthy": True}))
    assert "HALT:BROKER_UNREACHABLE" in assess(tmp_path, now_ms=10_000)["reasons"]


def test_driver_heartbeat_writes_evidence(tmp_path):
    import subprocess

    worker = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        _heartbeat(tmp_path, worker)
    finally:
        worker.stdin.close()
        worker.stdout.close()
        worker.terminate()
        worker.wait()
    record = json.loads((tmp_path / "health" / "guardian" / "heartbeat.json").read_text())
    assert record["schema"] == "guardian_heartbeat_v1"
    assert record["halts"] == []
    assert type(record["heartbeat_ms"]) is int
