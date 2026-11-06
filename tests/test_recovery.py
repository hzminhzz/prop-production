import multiprocessing
import os
import signal
import sqlite3

import pytest
from test_guardian import setup_system


def kill_process(tmp_path, boundary):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = guardian.decide(proposal, __import__("test_planner").quote(), 301000)
    def crash(name):
        if name == boundary:
            os.kill(os.getpid(), signal.SIGKILL)
    guardian.fault = crash
    if boundary in {"before_begin", "before_commit", "after_commit"}:
        journal.fault = crash
    ids = guardian.reserve(decision.data["decision_id"], 301000)
    if boundary in {"before_cancel", "after_cancel"}:
        broker.fill_on_submit = False
    guardian.dispatch(ids[0], 301000)
    if boundary in {"before_cancel", "after_cancel"}:
        broker.fault_mode = "PARTIAL_DURING_CANCEL"
        guardian.cancel(ids[0], 301001)


@pytest.mark.parametrize("boundary", [
    "before_begin", "before_commit", "after_commit", "before_reservation", "after_reservation",
    "before_send", "after_send", "after_ack", "before_cancel", "after_cancel",
])
def test_actual_sigkill_never_replays_opening_or_loses_pending_risk(tmp_path, boundary):
    from prop_production.journal import Journal
    from prop_production.judge.broker import FakeBroker
    child = multiprocessing.get_context("fork").Process(target=kill_process, args=(tmp_path, boundary))
    child.start()
    child.join(10)
    assert child.exitcode == -signal.SIGKILL
    with Journal(tmp_path / "account.sqlite", "fixture-account") as journal:
        state = journal.snapshot()
        assert state["runtime"]["permit"] is None
        assert state["runtime"]["state"] == "DISARMED"
        assert state["runtime"]["boot_epoch"] == 2
        broker = FakeBroker(tmp_path / "broker.sqlite")
        submits = [a for a in broker.audit() if a["kind"] == "SUBMIT"]
        assert len(submits) <= 1
        for submitted in submits:
            assert submitted["intent_id"] in state["intents"]
            assert state["reservations"]
            assert submitted["commit_sequence"] <= state["sequence"]
        broker.close()
        journal.verify()


def test_sqlite_real_full_error_latches_storage_without_dispatch(tmp_path):
    from prop_production.journal import Journal, StorageUnavailable
    with Journal(tmp_path / "full.sqlite", "fixture-account", initialize=True) as journal:
        current = journal.con.execute("PRAGMA page_count").fetchone()[0]
        journal.con.execute(f"PRAGMA max_page_count={current}")
        with pytest.raises(StorageUnavailable):
            journal.update("FULL", 1, lambda s: s.update(large_blob="X" * 100000))
        assert not journal.storage_healthy
        assert "large_blob" not in journal.snapshot()
    with Journal(tmp_path / "full.sqlite", "fixture-account") as journal:
        assert journal.snapshot()["runtime"]["permit"] is None


@pytest.mark.parametrize("outage", [45000, 120000])
def test_provider_outage_never_reports_false_flat_after_prior_flat(tmp_path, outage):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    assert journal.snapshot()["runtime"]["flat_confirmed"] is True
    broker.connected = False
    result = guardian.reconcile(301000 + outage)
    assert result == {"state": "UNRESOLVED", "certainty": "UNKNOWN", "flat_confirmed": False}
    journal.close()
    with __import__("prop_production.journal", fromlist=["Journal"]).Journal(
            tmp_path / "account.sqlite", "fixture-account") as resumed:
        assert "BROKER_UNREACHABLE" in resumed.snapshot()["halts"]
        assert resumed.snapshot()["runtime"]["permit"] is None
    broker.close()
    floor.close()


def test_cancel_with_partial_fill_keeps_reservation_until_authoritative_proof(tmp_path):
    from test_planner import quote
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    receipt = guardian.decide(proposal, quote(), 301000)
    ids = guardian.reserve(receipt.data["decision_id"], 301000)
    broker.fill_on_submit = False
    guardian.dispatch(ids[0], 301000)
    broker.fault_mode = "PARTIAL_DURING_CANCEL"
    guardian.cancel(ids[0], 301001)
    assert journal.snapshot()["reservations"]
    result = guardian.reconcile(301002)
    state = journal.snapshot()
    assert state["intents"][ids[0]]["status"] == "CANCELLED"
    assert state["positions"]["BTC"] != "0"
    assert result["flat_confirmed"] is False
    assert not state["reservations"]
    journal.close()
    broker.close()
    floor.close()


def test_repeated_stale_rest_is_not_cutoff_and_backup_truncation_rejected(tmp_path):
    from prop_production.journal import Journal, StorageUnavailable
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    broker.history_complete = False
    for now in (301001, 301002):
        assert guardian.reconcile(now)["certainty"] == "UNKNOWN"
    proof = journal.backup(tmp_path / "backup.sqlite")
    (tmp_path / "backup.sqlite").write_bytes(b"SQLite format 3")
    with pytest.raises(StorageUnavailable):
        Journal.restore(tmp_path / "backup.sqlite", tmp_path / "restore.sqlite", proof)
    journal.close()
    broker.close()
    floor.close()


def test_long_reader_does_not_turn_busy_checkpoint_into_financial_failure(tmp_path):
    from prop_production.journal import Journal
    with Journal(tmp_path / "wal.sqlite", "fixture-account", initialize=True) as journal:
        reader = sqlite3.connect(journal.path)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM snapshot").fetchall()
        for n in range(30):
            journal.update("HEALTH", n, lambda s: s["runtime"].update(last_progress_ms=n))
        journal.checkpoint()
        assert journal.storage_healthy
        reader.close()
        journal.checkpoint()
        journal.verify()
