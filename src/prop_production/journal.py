"""One financial writer; atomic hash-chained snapshots and durable outbox state."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import sqlite3
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evidence import canonical, digest, load_json
from .financial_semantics import CONTRACT as SEMANTIC_V2
from .financial_semantics import validate_transition

ZERO = "0" * 64
State = dict[str, Any]


class StorageUnavailable(RuntimeError):
    """HALT_STORAGE: callers must revoke dispatch, including after transient failures."""


def initial_state(account_binding: str) -> State:
    return {
        "schema": "guardian_journal_v1",
        "_semantic_contract": SEMANTIC_V2,
        "account_binding": account_binding,
        "sequence": 0,
        "finance": {},
        "halts": [],
        "decisions": {},
        "intents": {},
        "positions": {},
        "pod_positions": {},
        "fills": {},
        "reservations": {},
        "source_cursors": {},
        "runtime": {
            "state": "DISARMED",
            "boot_epoch": 0,
            "broker_epoch": 0,
            "certainty": "UNKNOWN",
            "permit": None,
            "last_progress_ms": None,
        },
    }


def _verify(
    con: sqlite3.Connection,
    *,
    finance_history: deque[dict[str, Any]] | None = None,
) -> tuple[State, int, str]:
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
        expected = digest(
            {"seq": seq, "kind": kind, "at_ms": at_ms, "payload": parsed, "prev": prev}
        )
        if expected != row_sha or parsed["sequence"] != seq:
            raise ValueError("JOURNAL_TAMPER")
        if parsed.get("_semantic_contract") == SEMANTIC_V2:
            validate_transition(state, parsed, kind, previous_head=previous)
        elif state.get("_semantic_contract") == SEMANTIC_V2:
            raise ValueError("JOURNAL_SEMANTIC_VERSION_DOWNGRADE")
        previous, state, sequence = row_sha, parsed, seq
        if finance_history is not None and kind == "RECONCILE":
            finance = parsed.get("finance")
            if isinstance(finance, dict) and type(finance.get("observed_at_ms")) is int:
                finance_history.append(
                    {
                        "journal_sequence": seq,
                        "reconciled_at_ms": at_ms,
                        "observed_at_ms": finance["observed_at_ms"],
                        "release_sha": parsed.get("active_release_sha"),
                        "equity": finance.get("equity"),
                        "unrealized": finance.get("unrealized"),
                        "cash": finance.get("cash"),
                        "risk_floor": finance.get("risk_floor"),
                        "daily_floor": finance.get("daily_floor"),
                        "internal_floor": finance.get("internal_floor"),
                        "trailing_floor": finance.get("trailing_floor"),
                        "headroom": finance.get("headroom"),
                        "day": finance.get("day"),
                        "realized_made": finance.get("realized_made"),
                    }
                )
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
        # In-process commit capability, granted only after an independent
        # authenticated recovery request and broker proof have both passed.
        self._verified_recovery_commit: tuple[str, str, str, int] | None = None
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
        runtime.update(
            state="DISARMED",
            certainty="UNKNOWN",
            permit=None,
            boot_epoch=runtime["boot_epoch"] + 1,
            broker_epoch=runtime["broker_epoch"] + 1,
        )
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
        if kind.startswith("RECOVER_AUTH_"):
            recovery = state.get("runtime", {}).get("last_recovery", {})
            if self._verified_recovery_commit != (
                kind,
                recovery.get("request_sha"),
                recovery.get("remediation_sha"),
                expected_sequence,
            ):
                raise ValueError("GUARDIAN_SEMANTIC_UNAUTHORIZED_HALT_CLEARANCE")
        new_state = copy.deepcopy(state)
        sequence = self._seq + 1
        new_state["sequence"] = sequence
        if new_state.get("_semantic_contract") == SEMANTIC_V2:
            validate_transition(self._state, new_state, kind, previous_head=self._head)
        elif self._state.get("_semantic_contract") == SEMANTIC_V2:
            raise ValueError("JOURNAL_SEMANTIC_VERSION_DOWNGRADE")
        elif kind in {"RESERVE_AND_OUTBOX", "ARM", "DISPATCHING", "COMMIT_INTENT"}:
            raise ValueError("GUARDIAN_LEGACY_FINANCE_REQUIRES_FORWARD_CHECKPOINT")
        payload = canonical(new_state).decode()
        if len(payload) > 8_000_000:
            self.storage_healthy = False
            raise StorageUnavailable("HALT_STORAGE_CAPACITY")
        row_sha = digest(
            {
                "seq": sequence,
                "kind": kind,
                "at_ms": at_ms,
                "payload": new_state,
                "prev": self._head,
            }
        )
        try:
            if self.fault:
                self.fault("before_begin")
            self.con.execute("BEGIN IMMEDIATE")
            current = self.con.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            if current != expected_sequence or current != self._seq:
                raise ValueError("WRITER_FENCE_MISMATCH")
            self.con.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (sequence, kind, at_ms, payload, self._head, row_sha),
            )
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

    def recover(
        self,
        reason: str,
        at_ms: int,
        *,
        operator: str,
        incident_id: str,
        evidence_sha: str,
        reset_daily_baseline: bool = False,
    ) -> int:
        """Evidence-gated recovery: HALTED → RECOVERY_REQUESTED → RECOVERY_VERIFIED.

        Requires the exact existing HALT, operator identity, incident ID and a
        well-formed evidence digest. Unrelated HALTs keep the runtime HALTED;
        clearing the last one transitions to DISARMED with a fresh epoch and
        revoked permits. Never auto-arms.

        reset_daily_baseline: for MIDNIGHT_BASELINE_UNKNOWN only — a skipped
        UTC day can never satisfy the +1 rollover, so the stale baseline is
        discarded (audited here) and the next boundary-aligned reconcile
        bootstraps fresh from provider evidence.
        """
        if self.snapshot()["account_binding"] != "fixture-account":
            # SHA-shaped text is not remediation of a protected financial HALT.
            raise ValueError("PROTECTED_RECOVERY_REQUIRES_SIGNED_CAUSE_SPECIFIC_PROOF")
        if reason not in self.snapshot()["halts"]:
            raise ValueError(f"HALT_NOT_PRESENT: {reason}")
        if reset_daily_baseline and reason != "MIDNIGHT_BASELINE_UNKNOWN":
            raise ValueError("DAILY_BASELINE_RESET_ONLY_FOR_MIDNIGHT_INCIDENTS")
        if not operator.strip() or not incident_id.strip():
            raise ValueError("RECOVERY_IDENTITY_REQUIRED")
        if (
            not isinstance(evidence_sha, str)
            or len(evidence_sha) != 64
            or any(c not in "0123456789abcdef" for c in evidence_sha.lower())
        ):
            raise ValueError("RECOVERY_EVIDENCE_DIGEST_INVALID")

        def apply(state: State) -> None:
            state["halts"].remove(reason)
            runtime = state["runtime"]
            # Unrelated HALTs keep the runtime halted; only a clean sheet disarms.
            runtime["state"] = "DISARMED" if not state["halts"] else "HALTED"
            runtime["boot_epoch"] = runtime.get("boot_epoch", 0) + 1
            runtime["permit"] = None
            if reset_daily_baseline:
                state["finance"] = {}  # stale baseline discarded; boundary re-bootstraps
            runtime["last_recovery"] = {
                "recovery_state": "RECOVERY_VERIFIED",
                "reason": reason,
                "operator": operator,
                "incident_id": incident_id,
                "evidence_sha256": evidence_sha.lower(),
                "at_ms": at_ms,
                "daily_baseline_reset": reset_daily_baseline,
                "remaining_halts": list(state["halts"]),
            }

        return self.update("RECOVER_" + reason, at_ms, apply)

    def recover_authenticated(
        self,
        signed_request: dict[str, Any],
        *,
        recovery_key: Any,
        gateway: Any,
        now_ms: int,
        revoked_key_shas: set[str] | None = None,
        expected_recovery_key_sha: str | None = None,
        revoked_nonces: set[str] | None = None,
        forbidden_key_shas: set[str] | None = None,
    ) -> int:
        """Clear one HALT after protected signature *and* independent broker proof.

        The gateway read happens before SQLite BEGIN. A signed operator request
        never substitutes for verified provider history, conservation or scope.
        Production keys/trust pins are injected externally; none are provisioned.
        """
        import re

        from .evidence import digest as evidence_digest
        from .evidence import verify as verify_evidence
        from .recovery import require_real_remediation

        if not hasattr(recovery_key, "public_bytes_raw"):
            raise ValueError("RECOVERY_PROTECTED_SIGNER_REQUIRED")
        key_sha = hashlib.sha256(recovery_key.public_bytes_raw()).hexdigest()
        prior = self.snapshot()
        if prior["account_binding"] != "fixture-account" and (
            expected_recovery_key_sha != key_sha or not forbidden_key_shas
        ):
            raise ValueError("RECOVERY_PROTECTED_KEY_TRUST_FLOOR_MISSING")
        if key_sha in (revoked_key_shas or set()) or key_sha in (forbidden_key_shas or set()):
            raise ValueError("RECOVERY_SIGNER_REVOKED_OR_WRONG_AUTHORITY")
        request = verify_evidence(signed_request, recovery_key, "guardian-recovery-operator")
        runtime = prior["runtime"]
        nonce = request.get("nonce")
        reason = request.get("reason")
        if (
            request.get("schema") != "guardian_recovery_request_v2"
            or request.get("role") != "protected_guardian_recovery"
            or request.get("account_binding") != prior["account_binding"]
            or request.get("journal_head") != self._head
            or request.get("journal_sequence") != self._seq
            or request.get("boot_epoch") != runtime["boot_epoch"]
            or request.get("release_sha") != prior.get("active_release_sha")
            or request.get("recovery_key_sha") != key_sha
            or not isinstance(nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
            or nonce in (revoked_nonces or set())
            or nonce in prior.get("recovery_nonces", {})
            or type(request.get("issued_at_ms")) is not int
            or type(request.get("expires_at_ms")) is not int
            or type(now_ms) is not int
            or not request["issued_at_ms"] <= now_ms < request["expires_at_ms"]
            or not 0 < request["expires_at_ms"] - request["issued_at_ms"] <= 900_000
            or not isinstance(request.get("operator"), str)
            or not request["operator"].strip()
            or not isinstance(request.get("incident_id"), str)
            or not request["incident_id"].strip()
        ):
            raise ValueError("RECOVERY_REQUEST_SCOPE_EXPIRY_OR_REPLAY")
        if not isinstance(reason, str) or reason not in prior["halts"]:
            raise ValueError("RECOVERY_HALT_REASON_NOT_PRESENT")
        if request.get("reset_daily_baseline") is not False:
            raise ValueError("RECOVERY_FINANCIAL_BASELINE_RESET_NOT_VERIFIED")

        # The protected BrokerGateway owns provider authentication and scope.
        # An unavailable/coherently incomplete provider cannot clear a HALT.
        provider_snapshot = gateway.observe(now_ms)
        proof_sha = require_real_remediation(
            reason=reason, state=prior, snapshot=provider_snapshot, now_ms=now_ms
        )
        if request.get("remediation_sha") != proof_sha:
            raise ValueError("RECOVERY_INDEPENDENT_REMEDIATION_DIGEST_MISMATCH")

        next_state = self.snapshot()
        next_state["halts"].remove(reason)
        new_runtime = next_state["runtime"]
        new_runtime["state"] = "HALTED" if next_state["halts"] else "DISARMED"
        new_runtime["permit"] = None
        new_runtime["boot_epoch"] += 1
        new_runtime["broker_epoch"] += 1
        next_state.setdefault("recovery_nonces", {})[nonce] = {
            "request_sha": evidence_digest(request),
            "head_at_request": self._head,
            "remediation_sha": proof_sha,
        }
        new_runtime["last_recovery"] = {
            "recovery_state": "RECOVERY_VERIFIED",
            "account_binding": prior["account_binding"],
            "reason": reason,
            "operator": request["operator"],
            "incident_id": request["incident_id"],
            "request_sha": evidence_digest(request),
            "remediation_sha": proof_sha,
            "recovery_key_sha": key_sha,
            "at_ms": now_ms,
            "remaining_halts": list(next_state["halts"]),
        }
        recovery_kind = "RECOVER_AUTH_" + reason
        self._verified_recovery_commit = (
            recovery_kind,
            evidence_digest(request),
            proof_sha,
            self._seq,
        )
        try:
            return self.commit(
                recovery_kind,
                now_ms,
                next_state,
                expected_sequence=self._seq,
            )
        finally:
            self._verified_recovery_commit = None

    def append_semantic_checkpoint(
        self,
        *,
        gateway: Any,
        now_ms: int,
        expected_head: str,
    ) -> int:
        """Add v2 semantic admission only after genuine independent provider proof.

        Entire old v1 event bytes and snapshot history are preserved. This
        records new forward assurance, NOT a restatement of old financial data
        or permission for protected trading.
        """
        from .recovery import require_real_remediation

        previous = self.snapshot()
        if previous.get("_semantic_contract") == SEMANTIC_V2:
            raise ValueError("GUARDIAN_SEMANTIC_CHECKPOINT_ALREADY_ESTABLISHED")
        if expected_head != self._head:
            raise ValueError("GUARDIAN_SEMANTIC_CHECKPOINT_HEAD_STALE")
        # Gateway I/O outside BEGIN, with coherent account/cutoff/finance,
        # terminal cumulative executions and no unresolved reservations.
        provider_snapshot = gateway.observe(now_ms)
        proof_sha = require_real_remediation(
            reason="RECONCILIATION_UNCERTAIN",
            state=previous,
            snapshot=provider_snapshot,
            now_ms=now_ms,
        )
        next_state = copy.deepcopy(previous)
        next_state["_semantic_contract"] = SEMANTIC_V2
        next_state["_semantic_checkpoint"] = {
            "schema": "guardian_semantic_checkpoint_v2",
            "legacy_head": self._head,
            "legacy_sequence": self._seq,
            "independent_remediation_sha": proof_sha,
            "provider_cutoff": provider_snapshot.cutoff,
            "provider_epoch": provider_snapshot.epoch,
            "source_release_sha": previous.get("active_release_sha"),
            "prior_halts": list(previous["halts"]),
            "original_obligations": {
                "intents": len(previous["intents"]),
                "fills": len(previous["fills"]),
                "reservations": len(previous["reservations"]),
                "positions": len(previous["positions"]),
            },
        }
        next_state["runtime"].update(
            state="DISARMED" if not previous["halts"] else "HALTED",
            permit=None,
            boot_epoch=previous["runtime"]["boot_epoch"] + 1,
            broker_epoch=previous["runtime"]["broker_epoch"] + 1,
        )
        return self.commit(
            "SEMANTIC_CHECKPOINT_V2",
            now_ms,
            next_state,
            expected_sequence=self._seq,
        )

    def verify(self) -> dict[str, Any]:
        if self.con is None:
            raise StorageUnavailable("JOURNAL_CLOSED")
        checked, seq, head = _verify(self.con)
        semantic = (
            "FORWARD_V2_LEGACY_HISTORY_PRESERVED"
            if checked.get("_semantic_checkpoint")
            else "VERIFIED_V2"
            if checked.get("_semantic_contract") == SEMANTIC_V2
            else "LEGACY_UNVERIFIABLE_V1"
        )
        return {"integrity": "VERIFIED", "sequence": seq, "head": head, "semantic_status": semantic}

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
        return {
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "sequence": seq,
            "head": head,
            "account_binding": self._state["account_binding"],
            "release_sha": self._state.get("active_release_sha"),
        }

    @staticmethod
    def restore(source: Path, destination: Path, proof: dict[str, Any]) -> None:
        if source.is_symlink() or destination.exists():
            raise StorageUnavailable("RESTORE_PATH_INVALID")
        if hashlib.sha256(source.read_bytes()).hexdigest() != proof["sha256"]:
            raise StorageUnavailable("BACKUP_DIGEST_MISMATCH")
        restored = Journal.read(source)
        if (
            restored["sequence"] != proof["sequence"]
            or restored["account_binding"] != proof["account_binding"]
            or restored.get("active_release_sha") != proof.get("release_sha")
        ):
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

    @staticmethod
    def read_with_finance_history(
        path: Path,
        *,
        limit: int = 256,
    ) -> tuple[State, list[dict[str, Any]]]:
        """Owner-only full-chain verification; never call on a dashboard refresh.

        Retain recent authentic RECONCILE observations, including contractual
        floors and day boundaries. This does not reconstruct missing history.
        """
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("INVALID_FINANCE_HISTORY_LIMIT")
        if path.is_symlink() or not path.is_file():
            raise StorageUnavailable("JOURNAL_UNAVAILABLE")
        con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, isolation_level=None)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            history: deque[dict[str, Any]] = deque(maxlen=limit)
            state, _, _ = _verify(con, finance_history=history)
            return state, list(history)
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
