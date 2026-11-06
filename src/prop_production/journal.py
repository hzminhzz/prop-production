"""One financial writer; atomic hash-chained snapshots and durable outbox state."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evidence import canonical, digest, load_json

ZERO = "0" * 64
State = dict[str, Any]


class StorageUnavailable(RuntimeError):
    """HALT_STORAGE: callers must revoke dispatch, including after transient failures."""


def initial_state(account_binding: str) -> State:
    return {
        "schema": "guardian_journal_v1", "account_binding": account_binding, "sequence": 0,
        "finance": {}, "halts": [], "decisions": {}, "intents": {}, "positions": {},
        "pod_positions": {}, "fills": {}, "reservations": {}, "source_cursors": {},
        "runtime": {"state": "DISARMED", "boot_epoch": 0, "broker_epoch": 0,
                    "certainty": "UNKNOWN", "permit": None, "last_progress_ms": None},
    }


def _verify(con: sqlite3.Connection) -> tuple[State, int, str]:
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("CORRUPT_DATABASE")
    if con.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("FOREIGN_KEY_FAILURE")
    previous = ZERO
    state: State = {}
    sequence = 0
    for seq, kind, at_ms, payload, prev, row_sha in con.execute(
        "SELECT seq,kind,at_ms,payload,prev,sha FROM events ORDER BY seq"
    ):
        if seq != sequence + 1 or prev != previous:
            raise ValueError("JOURNAL_CHAIN_GAP")
        parsed = load_json(payload)
        expected = digest({"seq": seq, "kind": kind, "at_ms": at_ms,
                           "payload": parsed, "prev": prev})
        if expected != row_sha or parsed["sequence"] != seq:
            raise ValueError("JOURNAL_TAMPER")
        previous, state, sequence = row_sha, parsed, seq
    snap = con.execute("SELECT seq,payload,sha FROM snapshot WHERE id=1").fetchone()
    if sequence and (snap is None or snap != (sequence, canonical(state).decode(), previous)):
        raise ValueError("SNAPSHOT_MISMATCH")
    if not sequence and snap is not None:
        raise ValueError("ORPHAN_SNAPSHOT")
    return state, sequence, previous


class Journal:
    def __init__(self, path: Path, account_binding: str, *, initialize: bool = False):
        self.path = path
        self.storage_healthy = False
        self.fault: Callable[[str], None] | None = None
        self.con: sqlite3.Connection | None = None
        self.lock_fd: int | None = None
        self._state: State = {}
        self._seq = 0
        self._head = ZERO
        try:
            if path.is_symlink() or not path.parent.is_dir() or not account_binding:
                raise ValueError("UNSAFE_JOURNAL_PATH")
            if not initialize and not path.is_file():
                raise ValueError("MISSING_JOURNAL")
            lock_path = path.with_suffix(path.suffix + ".writer.lock")
            self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.con = sqlite3.connect(path, timeout=0.1, isolation_level=None)
            self.con.execute("PRAGMA busy_timeout=100")
            self.con.execute("PRAGMA foreign_keys=ON")
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=FULL")
            if initialize:
                self.con.executescript(
                    "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
                    "at_ms INTEGER NOT NULL,payload TEXT NOT NULL,prev TEXT NOT NULL,sha TEXT NOT NULL);"
                    "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1),"
                    "seq INTEGER NOT NULL REFERENCES events(seq),payload TEXT NOT NULL,sha TEXT NOT NULL);"
                )
                os.chmod(path, 0o600)
            self._state, self._seq, self._head = _verify(self.con)
            if self._seq == 0:
                if not initialize:
                    raise ValueError("EMPTY_JOURNAL")
                self._state = initial_state(account_binding)
            if self._state["account_binding"] != account_binding:
                raise ValueError("ACCOUNT_MISMATCH")
            self.storage_healthy = True
            self.update("BOOT_DISARMED", 0, self._restart)
        except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
            self.close()
            raise StorageUnavailable("HALT_STORAGE") from exc

    @staticmethod
    def _restart(state: State) -> None:
        runtime = state["runtime"]
        runtime.update(state="DISARMED", certainty="UNKNOWN", permit=None,
                       boot_epoch=runtime["boot_epoch"] + 1,
                       broker_epoch=runtime["broker_epoch"] + 1)
        for intent in state["intents"].values():
            if intent["status"] == "DISPATCHING":
                intent["status"] = "UNKNOWN"
                if "UNKNOWN_OUTCOME" not in state["halts"]:
                    state["halts"].append("UNKNOWN_OUTCOME")

    @property
    def in_transaction(self) -> bool:
        return bool(self.con is not None and self.con.in_transaction)

    @property
    def sequence(self) -> int:
        return self._seq

    @property
    def head(self) -> str:
        return self._head

    def snapshot(self) -> State:
        return copy.deepcopy(self._state)

    def update(self, kind: str, at_ms: int, change: Callable[[State], None]) -> int:
        # The callback runs BEFORE BEGIN. Only SQL and deterministic fault boundaries
        # occur inside the financial transaction; no broker callback is accepted.
        state = self.snapshot()
        change(state)
        return self.commit(kind, at_ms, state, expected_sequence=self._seq)

    def commit(self, kind: str, at_ms: int, state: State, *, expected_sequence: int) -> int:
        if not self.storage_healthy or self.con is None:
            raise StorageUnavailable("HALT_STORAGE")
        if state["account_binding"] != self._state["account_binding"]:
            raise ValueError("ACCOUNT_MISMATCH")
        new_state = copy.deepcopy(state)
        sequence = self._seq + 1
        new_state["sequence"] = sequence
        payload = canonical(new_state).decode()
        if len(payload) > 8_000_000:
            self.storage_healthy = False
            raise StorageUnavailable("HALT_STORAGE_CAPACITY")
        row_sha = digest({"seq": sequence, "kind": kind, "at_ms": at_ms,
                          "payload": new_state, "prev": self._head})
        try:
            if self.fault:
                self.fault("before_begin")
            self.con.execute("BEGIN IMMEDIATE")
            current = self.con.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            if current != expected_sequence or current != self._seq:
                raise ValueError("WRITER_FENCE_MISMATCH")
            self.con.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                             (sequence, kind, at_ms, payload, self._head, row_sha))
            self.con.execute(
                "INSERT INTO snapshot VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "seq=excluded.seq,payload=excluded.payload,sha=excluded.sha",
                (sequence, payload, row_sha),
            )
            if self.fault:
                self.fault("before_commit")
            self.con.execute("COMMIT")
            self._state, self._seq, self._head = new_state, sequence, row_sha
            if self.fault:
                self.fault("after_commit")
            return sequence
        except (OSError, sqlite3.Error, ValueError) as exc:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            self.storage_healthy = False
            raise StorageUnavailable("HALT_STORAGE") from exc

    def halt(self, reason: str, at_ms: int) -> int:
        def apply(state: State) -> None:
            if reason not in state["halts"]:
                state["halts"].append(reason)
            state["runtime"].update(state="HALTED", permit=None)
        return self.update("HALT_" + reason, at_ms, apply)

    def recover(self, reason: str, at_ms: int, *, operator: str, incident_id: str,
                evidence_sha: str) -> int:
        """Evidence-gated recovery: HALTED → RECOVERY_REQUESTED → RECOVERY_VERIFIED.

        Requires the exact existing HALT, operator identity, incident ID and a
        well-formed evidence digest. Unrelated HALTs keep the runtime HALTED;
        clearing the last one transitions to DISARMED with a fresh epoch and
        revoked permits. Never auto-arms.
        """
        if reason not in self.snapshot()["halts"]:
            raise ValueError(f"HALT_NOT_PRESENT: {reason}")
        if not operator.strip() or not incident_id.strip():
            raise ValueError("RECOVERY_IDENTITY_REQUIRED")
        if (not isinstance(evidence_sha, str) or len(evidence_sha) != 64
                or any(c not in "0123456789abcdef" for c in evidence_sha.lower())):
            raise ValueError("RECOVERY_EVIDENCE_DIGEST_INVALID")

        def apply(state: State) -> None:
            state["halts"].remove(reason)
            runtime = state["runtime"]
            # Unrelated HALTs keep the runtime halted; only a clean sheet disarms.
            runtime["state"] = "DISARMED" if not state["halts"] else "HALTED"
            runtime["boot_epoch"] = runtime.get("boot_epoch", 0) + 1
            runtime["permit"] = None
            runtime["last_recovery"] = {
                "recovery_state": "RECOVERY_VERIFIED", "reason": reason,
                "operator": operator, "incident_id": incident_id,
                "evidence_sha256": evidence_sha.lower(), "at_ms": at_ms,
                "remaining_halts": list(state["halts"]),
            }
        return self.update("RECOVER_" + reason, at_ms, apply)

    def verify(self) -> dict[str, Any]:
        if self.con is None:
            raise StorageUnavailable("JOURNAL_CLOSED")
        _, seq, head = _verify(self.con)
        return {"integrity": "VERIFIED", "sequence": seq, "head": head}

    def checkpoint(self) -> tuple[int, int, int]:
        if self.con is None or self.in_transaction:
            raise StorageUnavailable("CHECKPOINT_UNAVAILABLE")
        result = self.con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return (int(result[0]), int(result[1]), int(result[2]))

    def backup(self, destination: Path) -> dict[str, Any]:
        if self.con is None or self.in_transaction or destination.exists():
            raise StorageUnavailable("BACKUP_UNAVAILABLE")
        self.verify()
        target = sqlite3.connect(destination)
        try:
            self.con.backup(target)
            _, seq, head = _verify(target)
        finally:
            target.close()
        os.chmod(destination, 0o600)
        return {"sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "sequence": seq, "head": head, "account_binding": self._state["account_binding"],
                "release_sha": self._state.get("active_release_sha")}

    @staticmethod
    def restore(source: Path, destination: Path, proof: dict[str, Any]) -> None:
        if source.is_symlink() or destination.exists():
            raise StorageUnavailable("RESTORE_PATH_INVALID")
        if hashlib.sha256(source.read_bytes()).hexdigest() != proof["sha256"]:
            raise StorageUnavailable("BACKUP_DIGEST_MISMATCH")
        restored = Journal.read(source)
        if (restored["sequence"] != proof["sequence"] or
                restored["account_binding"] != proof["account_binding"] or
                restored.get("active_release_sha") != proof.get("release_sha")):
            raise StorageUnavailable("BACKUP_SCOPE_MISMATCH")
        src = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
        dst = sqlite3.connect(destination)
        try:
            _, _, head = _verify(src)
            if head != proof["head"]:
                raise StorageUnavailable("BACKUP_HEAD_MISMATCH")
            src.backup(dst)
            _verify(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(destination, 0o600)
        # A historical backup is not proof of current HWM or broker history.
        with Journal(destination, proof["account_binding"]) as recovered:
            recovered.halt("RESTORE_REQUIRES_INDEPENDENT_HISTORY", 0)

    @staticmethod
    def read(path: Path) -> State:
        if path.is_symlink() or not path.is_file():
            raise StorageUnavailable("JOURNAL_UNAVAILABLE")
        con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, isolation_level=None)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            state, _, _ = _verify(con)
            return state
        except (sqlite3.Error, ValueError) as exc:
            raise StorageUnavailable("HALT_STORAGE") from exc
        finally:
            con.close()

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        self.storage_healthy = False

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
