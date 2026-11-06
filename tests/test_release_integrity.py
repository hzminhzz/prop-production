"""Release receipts, partial failures, SIGKILL recovery and WAL-safe backups."""
import hashlib
import json
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from prop_production.compatibility import MANIFEST, check_dependency_constraints
from prop_production.deployment import (
    UNITS,
    _fresh_heartbeats,
    _release_lock,
    activate_release,
    backup_database,
    recover_activation,
    seal_bundle,
    verify_bundle,
)


def bundle(opt: Path, commit: str = "a" * 40) -> Path:
    release = opt / "releases" / commit
    for name in ("source", "connector", "units", "dist", "venv/bin"):
        (release / name).mkdir(parents=True, exist_ok=True)
    (release / "COMMIT").write_text(commit + "\n")
    (release / "TREE").write_text("b" * 40 + "\n")
    (release / "SOURCE.tar").write_bytes(b"fixture Git archive")
    archive_sha = hashlib.sha256(b"fixture Git archive").hexdigest()
    (release / "SOURCE.sha256").write_text(archive_sha + "  SOURCE.tar\n")
    (release / "compatibility.json").write_text(json.dumps(MANIFEST))
    (release / "source/fixture.py").write_text("FIXTURE = True\n")
    (release / "connector/provider.py").write_text("MODE = 'READ_ONLY'\n")
    (release / "dist/application.whl").write_bytes(b"fixture artifact")
    (release / "venv/bin/python").write_text("#!/bin/sh\nexit 0\n")
    (release / "venv/bin/python").chmod(0o755)
    for unit in UNITS:
        (release / "units" / unit).write_text("candidate unit: " + unit)
    seal_bundle(release)
    return release


def layout(tmp_path: Path):
    opt = tmp_path / "opt"
    candidate = bundle(opt)
    previous = opt / "releases" / ("c" * 40)
    previous.mkdir()
    (previous / "preserved").write_text("previous code")
    (opt / "current").symlink_to(previous)
    units = tmp_path / "units"
    units.mkdir()
    for unit in UNITS:
        (units / unit).write_text("old unit: " + unit)
    journal = tmp_path / "financial-journal"
    journal.write_bytes(b"do not rewind durable financial state")
    return opt, units, candidate, previous, journal


def test_complete_bundle_accepted(tmp_path):
    release = bundle(tmp_path)
    assert verify_bundle(release, expected_commit="a" * 40)["application_commit"] == "a" * 40


@pytest.mark.parametrize("path", [
    "COMMIT", "connector/provider.py", "venv/bin/python", "dist/application.whl",
    "compatibility.json", "SOURCE.tar",
])
def test_changed_artifact_or_identity_is_rejected(tmp_path, path):
    release = bundle(tmp_path)
    (release / path).write_text("changed")
    with pytest.raises((ValueError, OSError)):
        verify_bundle(release, expected_commit="a" * 40)


def test_added_importable_code_and_incomplete_bundle_are_rejected(tmp_path):
    release = bundle(tmp_path)
    (release / "connector/unreviewed.py").write_text("NEW_CODE = True")
    with pytest.raises(ValueError, match="CONTENT_DRIFT"):
        verify_bundle(release)
    (release / "BUNDLE.json").unlink()
    with pytest.raises(OSError):
        verify_bundle(release)


@pytest.mark.parametrize("fault", ["daemon-reload", "start", "health"])
def test_activation_failure_restores_pointer_units_and_active_set(tmp_path, fault):
    opt, units, candidate, previous, journal = layout(tmp_path)
    calls = []

    def service(arguments):
        calls.append(arguments)
        if arguments[0] == fault and (opt / "current").resolve() == candidate:
            raise RuntimeError("injected startup failure")

    def health():
        if fault == "health":
            raise RuntimeError("injected missing heartbeat")

    with pytest.raises(RuntimeError):
        activate_release(opt, units, candidate, previous_active=list(UNITS[:3]),
                         health_check=health, service=service)
    assert (opt / "current").resolve() == previous
    assert all((units / unit).read_text() == "old unit: " + unit for unit in UNITS)
    assert ["start", *UNITS[:3]] in calls
    assert json.loads((opt / "activation.json").read_text())["phase"] == "ROLLED_BACK"
    assert journal.read_bytes() == b"do not rewind durable financial state"
    assert verify_bundle(candidate)["application_commit"] == candidate.name


def test_successful_activation_keeps_previous_release(tmp_path):
    opt, units, candidate, previous, journal = layout(tmp_path)
    calls = []
    activate_release(opt, units, candidate, previous_active=list(UNITS),
                     health_check=lambda: None, service=calls.append)
    assert (opt / "current").resolve() == candidate
    assert (previous / "preserved").read_text() == "previous code"
    assert ["is-active", "--quiet", *UNITS] in calls
    assert json.loads((opt / "activation.json").read_text())["phase"] == "ACTIVE"
    assert journal.read_bytes() == b"do not rewind durable financial state"


def test_sigkill_after_pointer_switch_is_recoverable(tmp_path):
    opt, units, candidate, previous, journal = layout(tmp_path)
    program = """
import os, signal, sys
from pathlib import Path
from prop_production.deployment import UNITS, activate_release
opt, units, candidate = map(Path, sys.argv[1:])
def service(arguments):
    if arguments[0] == 'daemon-reload':
        os.kill(os.getpid(), signal.SIGKILL)
activate_release(opt, units, candidate, previous_active=list(UNITS),
                 health_check=lambda: None, service=service)
"""
    process = subprocess.run([sys.executable, "-c", program, str(opt), str(units), str(candidate)],
                             check=False, timeout=10)
    assert process.returncode == -signal.SIGKILL
    assert (opt / "current").resolve() == candidate
    assert json.loads((opt / "activation.json").read_text())["phase"] == "PREPARED"
    with pytest.raises(ValueError, match="RECOVERY_REQUIRED"):
        activate_release(opt, units, candidate, previous_active=[],
                         health_check=lambda: None, service=lambda _: None)
    calls = []
    recover_activation(opt, units, service=calls.append)
    assert (opt / "current").resolve() == previous
    assert all((units / unit).read_text() == "old unit: " + unit for unit in UNITS)
    assert journal.read_bytes() == b"do not rewind durable financial state"


def test_online_backup_includes_committed_wal_not_uncommitted_rows(tmp_path):
    source, target = tmp_path / "journal.sqlite", tmp_path / "backup.sqlite"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE events(value INTEGER)")
        conn.execute("INSERT INTO events VALUES (1)")
        conn.commit()
        conn.execute("INSERT INTO events VALUES (2)")
        assert source.with_name(source.name + "-wal").is_file()
        backup_database(source, target)
        with closing(sqlite3.connect(target)) as copied:
            assert copied.execute("SELECT value FROM events").fetchall() == [(1,)]
        conn.rollback()
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="BACKUP_PATH"):
        backup_database(source, target)


def test_activation_and_recovery_share_an_exclusive_lock(tmp_path):
    with _release_lock(tmp_path):
        with pytest.raises(BlockingIOError):
            with _release_lock(tmp_path):
                pytest.fail("a competing activation acquired the lock")
    with _release_lock(tmp_path):
        pass


def test_heartbeat_gate_waits_for_in_place_observer_write(tmp_path, monkeypatch):
    paths = [tmp_path / "health" / role / "heartbeat.json"
             for role in ("guardian", "observer", "bybit")]
    paths.append(tmp_path / "watchdog" / "heartbeat.json")
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text('{"trading_authorized":false}')
    observer = paths[1]
    observer.write_text("{")
    waits = []
    def finish_write(seconds):
        waits.append(seconds)
        observer.write_text('{"trading_authorized":false}')
    monkeypatch.setattr("prop_production.deployment.time.sleep", finish_write)
    _fresh_heartbeats(tmp_path, 0)
    assert len(waits) == 1
    observer.write_text('{"trading_authorized":true}')
    with pytest.raises(ValueError, match="UNEXPECTED_TRADING_AUTHORITY"):
        _fresh_heartbeats(tmp_path, 0)


def test_package_constraints_must_match_single_compatibility_manifest(tmp_path):
    project = Path(__file__).parents[1] / "pyproject.toml"
    check_dependency_constraints(project)
    changed = tmp_path / "pyproject.toml"
    changed.write_text(project.read_text().replace("ccxt==4.5.74", "ccxt==0.0.0"))
    with pytest.raises(ValueError, match="DEPENDENCY_DRIFT"):
        check_dependency_constraints(changed)
