"""Auditable prospective source evidence. Missing admission never fabricates paper fills."""
from __future__ import annotations

import fcntl
import os
import signal
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence import canonical, digest, load_json, require_sha
from .research_bridge import observe


class ObservationJournal:
    def __init__(self, path: Path, *, initialize: bool = False):
        if path.is_symlink() or not path.parent.is_dir() or (not initialize and not path.is_file()):
            raise ValueError("OBSERVATION_STORAGE")
        self.lock = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.con = sqlite3.connect(path, isolation_level=None, timeout=0.1)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        if initialize:
            self.con.execute("CREATE TABLE IF NOT EXISTS observations (seq INTEGER PRIMARY KEY,"
                             "known_ms INTEGER NOT NULL,payload TEXT NOT NULL,prev TEXT NOT NULL,sha TEXT NOT NULL)")
            os.chmod(path, 0o600)
        self.report(0)

    def append(self, evidence: dict[str, Any], known_ms: int) -> None:
        if evidence.get("schema") != "read_only_source_observation_v1" or type(known_ms) is not int or known_ms < 0:
            raise ValueError("OBSERVATION_SCHEMA")
        require_sha(evidence["native_tape_sha"])
        if evidence.get("source_nav_attested") is not False or evidence.get("trade_volume_complete") is not False:
            raise ValueError("OBSERVER_IS_NOT_ADMISSION_AUTHORITY")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            last = self.con.execute("SELECT seq,known_ms,sha FROM observations ORDER BY seq DESC LIMIT 1").fetchone()
            if last and known_ms < last[1]:
                raise ValueError("LOCAL_RECEIPT_CLOCK_REGRESSION")
            seq, previous = (last[0] + 1, last[2]) if last else (1, "0" * 64)
            body = {"seq": seq, "known_ms": known_ms, "evidence": evidence, "prev": previous}
            self.con.execute("INSERT INTO observations VALUES(?,?,?,?,?)",
                             (seq, known_ms, canonical(evidence).decode(), previous, digest(body)))
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise

    @staticmethod
    def _report(con: sqlite3.Connection, now_ms: int) -> dict[str, Any]:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("OBSERVATION_STORAGE_CORRUPT")
        previous = "0" * 64
        latest: dict[str, Any] = {}
        prior: dict[str, Any] = {}
        first = last = 0
        count = 0
        for seq, known, raw, prev, sha in con.execute("SELECT * FROM observations ORDER BY seq"):
            body = load_json(raw)
            if (seq != count + 1 or prev != previous or known < last
                    or sha != digest({"seq": seq, "known_ms": known, "evidence": body, "prev": prev})):
                raise ValueError("OBSERVATION_CHAIN_INVALID")
            if not count:
                first = known
            count, previous, last, prior, latest = seq, sha, known, latest, body
        alive = latest.get("upstream_alive") is True and 0 <= now_ms - last <= 90000
        quiet = bool(prior and prior["native_tape_sha"] == latest["native_tape_sha"])
        return {"schema": "prospective_paper_report_v1", "verdict": "BLOCKED_PAPER_ADMISSION",
                "observations": count, "first_local_known_ms": first if count else None,
                "last_local_known_ms": last if count else None, "head_sha": previous,
                "observed_elapsed_days": str(Decimal(last - first) / 86400000),
                "source_state": ("QUIET" if quiet else "RECEIPTS_ADVANCING") if alive else "DISCONNECTED_OR_UNKNOWN",
                "latest_evidence": latest, "synthetic_model_pnl": "NOT_COMPUTED",
                "observed_prospective_evidence": "RECEIPT_HASHES_AND_HEALTH_ONLY" if count else "NONE",
                "independently_matched_follower_pnl": "UNKNOWN", "paper_days_qualified": 0,
                "broker_mutations": 0, "simulated_fills": 0,
                "scientifically_qualified": False, "trading_authorized": False}

    def report(self, now_ms: int) -> dict[str, Any]:
        return self._report(self.con, now_ms)

    @staticmethod
    def read(path: Path, now_ms: int) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("OBSERVATION_STORAGE_MISSING")
        con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, isolation_level=None, timeout=0.1)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            return ObservationJournal._report(con, now_ms)
        finally:
            con.close()

    def close(self) -> None:
        self.con.close()
        os.close(self.lock)


def collect(path: Path, *, research_root: Path, research_python: Path, observer_root: Path,
            interval: int = 30, iterations: int = 1) -> dict[str, Any]:
    if not 5 <= interval <= 300 or iterations < 0:
        raise ValueError("OBSERVATION_SCHEDULE")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError("PRIVATE_OBSERVATION_DIRECTORY_REQUIRED")
    running = True
    def stop(*args: Any) -> None:
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    journal = ObservationJournal(path, initialize=not path.exists())
    next_due = time.monotonic()
    count = 0
    try:
        while running and (iterations == 0 or count < iterations):
            now = datetime.now(UTC)
            try:
                evidence = observe(research_root, research_python, observer_root, now.isoformat())
            except (OSError, ValueError, TimeoutError):
                evidence = {"schema": "read_only_source_observation_v1", "native_tape_sha": "0" * 64,
                    "source_receipts": 0, "market_receipts": 0, "collateral_receipts": 0,
                    "native_observer_health": "UNKNOWN", "upstream_alive": False,
                    "source_nav_attested": False, "trade_volume_complete": False,
                    "blockers": ["NATIVE_EVIDENCE_UNAVAILABLE"]}
            # This consumer's first-known clock is receipt time, never the source's event time.
            journal.append(evidence, time.time_ns() // 1000000)
            count += 1
            if iterations and count >= iterations:
                break
            next_due += interval
            while running and time.monotonic() < next_due:
                time.sleep(max(0, min(1, next_due - time.monotonic())))
        return journal.report(time.time_ns() // 1000000)
    finally:
        journal.close()
