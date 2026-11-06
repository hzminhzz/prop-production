"""Unit tests for the deployed observer, watchdog and driver modules."""
import json

from prop_production.driver import _heartbeat
from prop_production.observer import observation_record
from prop_production.watchdog import assess


def test_observation_record_flattens_dict_result():
    record = observation_record({"account_bound": True, "financial_complete": True,
                                 "account_source": "challenge",
                                 "financial": {"equity": {"value": "4999.225661"}}}, now_ms=1000)
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
    (health / "guardian" / "heartbeat.json").write_text(json.dumps({
        "heartbeat_ms": 9_000, "last_progress_ms": 9_000, "journal_sequence": 3,
        "certainty": "COHERENT", "halts": []}))
    (health / "observer" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000, "healthy": False}))
    assert "OBSERVER_UNHEALTHY" in assess(tmp_path, now_ms=10_000)["reasons"]


def test_watchdog_healthy_when_fresh(tmp_path):
    health = tmp_path / "health"
    (health / "guardian").mkdir(parents=True)
    (health / "observer").mkdir(parents=True)
    (health / "guardian" / "heartbeat.json").write_text(json.dumps({
        "heartbeat_ms": 9_000, "last_progress_ms": 9_000, "journal_sequence": 3,
        "certainty": "COHERENT", "halts": []}))
    (health / "observer" / "heartbeat.json").write_text(json.dumps({"heartbeat_ms": 9_000, "healthy": True}))
    assessment = assess(tmp_path, now_ms=10_000)
    # Market source is not connected: the honest ceiling is OPERATIONAL_BUT_BLOCKED.
    assert assessment["status"] == "OPERATIONAL_BUT_BLOCKED"
    assert "MARKET_SOURCE_UNKNOWN" in assessment["reasons"]
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
    assert record["schema"] == "guardian_heartbeat_v2"
    assert record["halts"] == []
    assert type(record["heartbeat_ms"]) is int


def test_recover_requires_existing_halt(tmp_path):
    from prop_production.journal import Journal
    journal = Journal(tmp_path / "account.sqlite", "fixture-account", initialize=True)
    try:
        journal.recover("BROKER_UNREACHABLE", 1000, operator="op",
                        incident_id="INC-1", evidence_sha="0" * 64)
        assert False, "recovery without a latched halt must fail"
    except ValueError as error:
        assert "HALT_NOT_PRESENT" in str(error)
    finally:
        journal.close()


def test_recover_requires_operator_and_incident(tmp_path):
    from prop_production.journal import Journal
    journal = Journal(tmp_path / "account.sqlite", "fixture-account", initialize=True)
    try:
        journal.halt("BROKER_UNREACHABLE", 1000)
        for kwargs in ({"operator": "", "incident_id": "INC-1", "evidence_sha": "0" * 64},
                       {"operator": "op", "incident_id": "", "evidence_sha": "0" * 64},
                       {"operator": "op", "incident_id": "INC-1", "evidence_sha": ""}):
            try:
                journal.recover("BROKER_UNREACHABLE", 1000, **kwargs)
                assert False, f"incomplete recovery evidence accepted: {kwargs}"
            except ValueError as error:
                assert "RECOVERY_EVIDENCE_INCOMPLETE" in str(error)
    finally:
        journal.close()


def test_recover_revokes_permits_and_advances_epoch(tmp_path):
    from prop_production.journal import Journal
    journal = Journal(tmp_path / "account.sqlite", "fixture-account", initialize=True)
    try:
        journal.halt("BROKER_UNREACHABLE", 1000)
        epoch_before = journal.snapshot()["runtime"]["boot_epoch"]
        journal.recover("BROKER_UNREACHABLE", 2000, operator="op",
                        incident_id="INC-2", evidence_sha="a" * 64)
        state = journal.snapshot()
        assert state["halts"] == []
        assert state["runtime"]["boot_epoch"] == epoch_before + 1  # fresh epoch, stale permits dead
        assert state["runtime"]["permit"] is None
        assert state["runtime"]["last_recovery"]["incident_id"] == "INC-2"
    finally:
        journal.close()
