import sqlite3

import pytest


def test_halt_finance_and_outbox_survive_restart(tmp_path):
    from prop_production.journal import Journal

    path = tmp_path / "account.sqlite"
    with Journal(path, "fixture-account", initialize=True) as journal:
        journal.update("MARK", 1000, lambda s: s["finance"].update(
            equity="9970", daily_baseline="10000", lifetime_hwm="10020", daily_epoch="2026-11-05"))
        journal.halt("MANUAL", 1001)
        before = journal.snapshot()
        backup = tmp_path / "backup.sqlite"
        proof = journal.backup(backup)
    with Journal(path, "fixture-account") as journal:
        after = journal.snapshot()
        assert after["finance"] == before["finance"]
        assert after["halts"] == ["MANUAL"]
        assert after["runtime"]["state"] == "DISARMED"
        assert after["runtime"]["boot_epoch"] > before["runtime"]["boot_epoch"]
        assert journal.verify()["integrity"] == "VERIFIED"
    Journal.restore(backup, tmp_path / "restored.sqlite", proof)
    with Journal(tmp_path / "restored.sqlite", "fixture-account") as restored:
        assert restored.snapshot()["halts"] == ["MANUAL", "RESTORE_REQUIRES_INDEPENDENT_HISTORY"]
        assert restored.snapshot()["runtime"]["permit"] is None


def test_second_writer_lock_busy_and_failed_commit_never_authorize(tmp_path):
    from prop_production.journal import Journal, StorageUnavailable

    path = tmp_path / "account.sqlite"
    with Journal(path, "fixture-account", initialize=True) as journal:
        with pytest.raises(StorageUnavailable):
            Journal(path, "fixture-account")
        other = sqlite3.connect(path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageUnavailable):
            journal.halt("MANUAL", 1001)
        assert journal.storage_healthy is False
        other.rollback()
        other.close()
        assert Journal.read(path)["halts"] == []


def test_corrupt_chain_and_wrong_account_are_rejected(tmp_path):
    from prop_production.journal import Journal, StorageUnavailable

    path = tmp_path / "account.sqlite"
    with Journal(path, "fixture-account", initialize=True) as journal:
        journal.halt("TRAILING", 1001)
    with pytest.raises(StorageUnavailable):
        Journal(path, "wrong-account")
    con = sqlite3.connect(path)
    con.execute("UPDATE events SET kind='ALTERED' WHERE seq=1")
    con.commit()
    con.close()
    with pytest.raises(StorageUnavailable):
        Journal(path, "fixture-account")


def test_failed_disk_commit_is_absent_and_latches_storage(tmp_path):
    from prop_production.journal import Journal, StorageUnavailable

    def disk_full(boundary):
        if boundary == "before_commit":
            raise sqlite3.OperationalError("database or disk is full")

    with Journal(tmp_path / "account.sqlite", "fixture-account", initialize=True) as journal:
        journal.fault = disk_full
        with pytest.raises(StorageUnavailable):
            journal.halt("MANUAL", 1001)
        assert journal.storage_healthy is False
        assert Journal.read(tmp_path / "account.sqlite")["halts"] == []
