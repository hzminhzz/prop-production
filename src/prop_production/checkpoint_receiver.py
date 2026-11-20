"""Protected V3 source intake, continuity and durable evidence; no order route."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .checkpoint_source import AUTHORITY as CHECKPOINT_AUTHORITY
from .checkpoint_source import GRID_MS, fields, integer, validate_pins, verify_transition
from .checkpoint_source import ZERO as CHECKPOINT_ZERO
from .compatibility import load_manifest
from .contracts import PORTFOLIO_RELEASE_V2
from .evidence import canonical, digest, load_json, require_sha, verify
from .release import (
    ReleaseFloor,
    ReleaseVerifier,
    VerifiedRelease,
    binding_sha,
    replay_release_binding,
    transition_release_binding,
)

MAX_PACKET_BYTES = 16_000_000
PACKET_NAME = re.compile(r"\d{20}-[0-9a-f]{16}\.json\Z")
ZERO = "0" * 64


def _read_pinned(path: Path, *, allow_group_read: bool = False) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("V4_PROTECTED_FILE_MISSING")
    if path.stat().st_mode & (0o027 if allow_group_read else 0o077):
        raise ValueError("V4_PROTECTED_FILE_MODE")
    if path.stat().st_size > MAX_PACKET_BYTES:
        raise ValueError("V4_PACKET_CAPACITY")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        data = os.read(fd, MAX_PACKET_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_PACKET_BYTES:
        raise ValueError("V4_PACKET_CAPACITY")
    return data


class CheckpointProjection:
    """Private source evidence only. No Guardian journal, permit or broker access."""

    def __init__(
        self,
        path: Path,
        *,
        release_sha: str,
        source_key: Ed25519PublicKey,
        pins: dict[str, Any],
        release: VerifiedRelease | None = None,
    ) -> None:
        fields(pins, {"policy", "scopes"})
        validate_pins(pins)
        self.pins = dict(load_json(canonical(pins)))
        self.key = source_key
        self.release = release
        self.path = path
        self.lock: int | None = None
        self.con: sqlite3.Connection | None = None
        if path.is_symlink() or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
            raise ValueError("CWSA_PRIVATE_RECEIVER_PATH")
        if not pins["scopes"]:
            raise ValueError("CWSA_PINNED_SCOPES_REQUIRED")
        self.lock = os.open(
            path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.con = sqlite3.connect(path, isolation_level=None, timeout=0.1)
            tables = {
                r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and "cwsa_receiver_meta" not in tables:
                raise ValueError("CWSA_LEGACY_DATABASE_MIX")
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=FULL")
            self.con.executescript(
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_meta(id INTEGER PRIMARY KEY, binding TEXT);"
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_heads(source_id TEXT PRIMARY KEY,"
                " seq INTEGER NOT NULL,state TEXT NOT NULL,sha TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_events(source_id TEXT,seq INTEGER,"
                " payload TEXT NOT NULL,sha TEXT NOT NULL,PRIMARY KEY(source_id,seq));"
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_packets(sha TEXT PRIMARY KEY,"
                " payload TEXT NOT NULL,received_ms INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS cwsa_release_transitions(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL);"
            )
            binding = digest(
                {
                    "release_sha": release_sha,
                    "pins": pins,
                    "source_key": sha256(source_key.public_bytes_raw()).hexdigest(),
                }
            )
            row = self.con.execute("SELECT binding FROM cwsa_receiver_meta WHERE id=1").fetchone()
            if row is None:
                self.con.execute("INSERT INTO cwsa_receiver_meta VALUES(1,?)", (binding,))
            else:
                self._verify_release_bindings(row[0], binding)
            os.chmod(path, 0o600)
            if self.con.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("CWSA_RECEIVER_CORRUPT")
            self._verify_heads()
        except BaseException:
            self.close()
            raise

    def _verify_release_bindings(self, genesis: str, expected: str) -> None:
        assert self.con is not None
        previous, state, count = genesis, None, 0
        for sequence, raw in self.con.execute(
            "SELECT seq,payload FROM cwsa_release_transitions ORDER BY seq"
        ):
            if self.release is None or self.release.payload["schema"] != PORTFOLIO_RELEASE_V2:
                raise ValueError("CWSA_RELEASE_REBIND_CONTEXT")
            record = replay_release_binding(
                load_json(raw),
                authority_sha=self.release.payload["mandate_authority_sha"],
                source_key=self.key,
            )
            if (
                sequence != count + 1
                or record["predecessor"]["binding_sha"] != previous
                or (state is not None and record["predecessor"] != state)
            ):
                raise ValueError("CWSA_RELEASE_REBIND_CHAIN")
            state = record["successor"]
            previous, count = state["binding_sha"], sequence
        if previous != expected:
            raise ValueError("CWSA_RECEIVER_TRUST_DRIFT")

    def rebind(self, release: VerifiedRelease, key: Ed25519PublicKey, *, now_ms: int) -> None:
        assert self.con is not None
        if self.release is None:
            raise ValueError("CWSA_RELEASE_REBIND_CONTEXT")
        if release.release_sha == self.release.release_sha:
            return
        record = transition_release_binding(self.release, release, key, self.key, at_ms=now_ms).data
        expected = binding_sha(self.release.release_sha, self.pins, self.key)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            genesis = self.con.execute(
                "SELECT binding FROM cwsa_receiver_meta WHERE id=1"
            ).fetchone()[0]
            self._verify_release_bindings(genesis, expected)
            count = self.con.execute("SELECT COUNT(*) FROM cwsa_release_transitions").fetchone()[0]
            self.con.execute(
                "INSERT INTO cwsa_release_transitions VALUES(?,?)",
                (count + 1, canonical(record).decode()),
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.release = release

    def _verify_heads(self) -> None:
        assert self.con is not None
        proofs: dict[tuple[str, int], str] = {}
        for packet_sha, raw, received_ms in self.con.execute("SELECT * FROM cwsa_receiver_packets"):
            if sha256(raw.encode()).hexdigest() != packet_sha:
                raise ValueError("CWSA_RESTART_PACKET_HASH")
            body = self._packet(raw.encode(), now_ms=received_ms)
            for event in body["transitions"]:
                verify_transition(
                    body["scope"],
                    body["policy"],
                    event,
                    body["originals"],
                    issued_ms=body["issued_ms"],
                )
                key = (body["scope"]["source_id"], event["sequence"])
                event_sha = digest(event)
                if key in proofs and proofs[key] != event_sha:
                    raise ValueError("CWSA_RESTART_EQUIVOCATION")
                proofs[key] = event_sha
        seen = set()
        for source, seq, state, head in self.con.execute("SELECT * FROM cwsa_receiver_heads"):
            previous = CHECKPOINT_ZERO
            current = None
            count = 0
            for number, payload, event_sha in self.con.execute(
                "SELECT seq,payload,sha FROM cwsa_receiver_events WHERE source_id=? ORDER BY seq",
                (source,),
            ):
                event = load_json(payload)
                if (
                    number != count + 1
                    or digest(event) != event_sha
                    or proofs.get((source, number)) != event_sha
                    or event["prior_sha"] != previous
                    or (current is not None and event["predecessor"] != current)
                ):
                    raise ValueError("CWSA_RECEIVER_CHAIN")
                seen.add((source, number))
                current, previous, count = event["successor"], event_sha, number
            if count != seq or current != load_json(state) or previous != head:
                raise ValueError("CWSA_RECEIVER_HEAD")
        if seen != set(proofs):
            raise ValueError("CWSA_RESTART_PROOF_COVERAGE")

    def _packet(self, raw: bytes, *, now_ms: int) -> dict[str, Any]:
        integer(now_ms)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("CWSA_PACKET_LIMIT")
        body = verify(load_json(raw), self.key, CHECKPOINT_AUTHORITY)
        fields(
            body,
            {
                "schema",
                "scope",
                "policy",
                "transitions",
                "originals",
                "issued_ms",
                "trading_authorized",
            },
        )
        if body["schema"] != "cwsa_checkpoint_packet_v3" or body["trading_authorized"] is not False:
            raise ValueError("CWSA_PACKET_VERSION")
        scope, policy = body["scope"], body["policy"]
        if scope != self.pins["scopes"].get(scope["source_id"]) or policy != self.pins["policy"]:
            raise ValueError("CWSA_PACKET_SCOPE_OR_POLICY")
        if integer(body["issued_ms"]) > now_ms:
            raise ValueError("CWSA_PACKET_FUTURE")
        transitions = body["transitions"]
        if not isinstance(transitions, list) or not 1 <= len(transitions) <= 256:
            raise ValueError("CWSA_PACKET_TRANSITION_LIMIT")
        if not isinstance(body["originals"], dict):
            raise ValueError("CWSA_ORIGINALS")
        for raw_sha, original in body["originals"].items():
            if not isinstance(original, str) or sha256(original.encode()).hexdigest() != raw_sha:
                raise ValueError("CWSA_ORIGINAL_HASH")
        return body

    def ingest(self, raw: bytes, *, now_ms: int) -> dict[str, Any]:
        assert self.con is not None
        body = self._packet(raw, now_ms=now_ms)
        scope, policy, transitions = body["scope"], body["policy"], body["transitions"]
        packet_sha = sha256(raw).hexdigest()
        if self.con.execute(
            "SELECT 1 FROM cwsa_receiver_packets WHERE sha=?", (packet_sha,)
        ).fetchone():
            return {
                "schema": "cwsa_protected_receiver_v3",
                "state": "DUPLICATE",
                "events": 0,
                "trading_authorized": False,
            }
        source = scope["source_id"]
        row = self.con.execute(
            "SELECT seq,state,sha FROM cwsa_receiver_heads WHERE source_id=?", (source,)
        ).fetchone()
        sequence, current, prior = (
            (0, None, CHECKPOINT_ZERO) if row is None else (row[0], load_json(row[1]), row[2])
        )
        validated = []
        for event in transitions:
            if integer(event["sequence"]) <= sequence:
                seen = self.con.execute(
                    "SELECT sha FROM cwsa_receiver_events WHERE source_id=? AND seq=?",
                    (source, event["sequence"]),
                ).fetchone()
                if seen != (digest(event),):
                    raise ValueError("CWSA_SOURCE_EQUIVOCATION")
                continue
            if event["sequence"] != sequence + 1 or event["prior_sha"] != prior:
                raise ValueError("CWSA_SOURCE_PREDECESSOR")
            if current is not None and event["predecessor"] != current:
                raise ValueError("CWSA_SOURCE_STATE")
            verify_transition(scope, policy, event, body["originals"], issued_ms=body["issued_ms"])
            sequence, current, prior = event["sequence"], event["successor"], digest(event)
            validated.append(event)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            for event in validated:
                self.con.execute(
                    "INSERT INTO cwsa_receiver_events VALUES (?,?,?,?)",
                    (source, event["sequence"], canonical(event).decode(), digest(event)),
                )
            if current is not None:
                self.con.execute(
                    "INSERT INTO cwsa_receiver_heads VALUES (?,?,?,?) ON CONFLICT(source_id)"
                    " DO UPDATE SET seq=excluded.seq,state=excluded.state,sha=excluded.sha",
                    (source, sequence, canonical(current).decode(), prior),
                )
            self.con.execute(
                "INSERT INTO cwsa_receiver_packets VALUES (?,?,?)",
                (packet_sha, raw.decode(), now_ms),
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return {
            "schema": "cwsa_protected_receiver_v3",
            "state": "EVIDENCE_COLLECTED",
            "events": len(validated),
            "trading_authorized": False,
        }

    def status(self) -> dict[str, Any]:
        assert self.con is not None
        heads = {
            r[0]: {"cursor": r[1], "phase": load_json(r[2])["phase"], "head": r[3]}
            for r in self.con.execute("SELECT * FROM cwsa_receiver_heads")
        }
        return {
            "schema": "cwsa_protected_receiver_v3",
            "source_count": len(heads),
            "sources": heads,
            "order_route": "NONE",
            "trading_authorized": False,
        }

    def outcomes(self, *, now_ms: int) -> list[dict[str, Any]]:
        assert self.con is not None
        integer(now_ms)
        result = []
        phases = {
            r[0]: load_json(r[1])["phase"]
            for r in self.con.execute("SELECT source_id,state FROM cwsa_receiver_heads")
        }
        for (payload,) in self.con.execute(
            "SELECT payload FROM cwsa_receiver_events ORDER BY source_id,seq"
        ):
            event = load_json(payload)
            outcome = event["effect"]
            if outcome is None:
                continue
            sample = outcome["sample"]
            deadline = outcome["checkpoint_ms"] + GRID_MS
            if sample is not None and sample["kind"] == "COMPLETE":
                deadline = min(
                    deadline,
                    min(r["state_ms"] for r in sample["receipts"])
                    + self.pins["policy"]["limits"]["max_state_age_ms"],
                )
            result.append(
                {
                    "outcome": outcome,
                    "source_cursor": event["sequence"],
                    "source_chain_sha": digest(event),
                    "deadline_ms": deadline,
                    "opening_candidate": phases[event["source_id"]] == "CONTINUOUS"
                    and outcome["kind"] == "CHANGED"
                    and outcome["published_ms"] <= now_ms < deadline,
                    "trading_authorized": False,
                }
            )
        return result

    def packet_for(self, outcome_id: str) -> dict[str, Any]:
        """Return the original signed envelope proving a committed source outcome."""
        assert self.con is not None
        require_sha(outcome_id)
        for (raw,) in self.con.execute("SELECT payload FROM cwsa_receiver_packets ORDER BY rowid"):
            envelope = load_json(raw)
            if any(
                t["effect"] is not None and t["effect"]["outcome_id"] == outcome_id
                for t in envelope["payload"]["transitions"]
            ):
                return dict(envelope)
        raise ValueError("CWSA_OUTCOME_PROOF_MISSING")

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


class CheckpointSourceReceiver:
    """A single protected CWSA V3 stream, pinned to its signed release."""

    wire_version = 3

    def __init__(
        self,
        path: Path,
        *,
        release: VerifiedRelease,
        source_key: Ed25519PublicKey,
        wire_version: int = 3,
        checkpoint_pins: dict[str, Any] | None = None,
    ):
        if (
            wire_version != self.wire_version
            or self.wire_version not in load_manifest()["contracts"]["source_wire_versions"]
        ):
            raise ValueError("CWSA_SOURCE_WIRE_VERSION")
        self.path = path
        self.release = release
        self.source_key = source_key
        if release.payload["schema"] == PORTFOLIO_RELEASE_V2:
            signed_pins = {
                "policy": release.payload["checkpoint_policy"],
                "scopes": release.payload["checkpoint_scopes"],
            }
            if checkpoint_pins is not None and checkpoint_pins != signed_pins:
                raise ValueError("CWSA_RELEASE_PIN_DRIFT")
            checkpoint_pins = signed_pins
        if checkpoint_pins is None:
            raise ValueError("CWSA_EXTERNAL_POLICY_PINS_REQUIRED")
        self.checkpoint = CheckpointProjection(
            path,
            release_sha=release.release_sha,
            source_key=source_key,
            pins=checkpoint_pins,
            release=release,
        )

    @property
    def pins(self) -> dict[str, Any]:
        return dict(load_json(canonical(self.checkpoint.pins)))

    def ingest(self, raw: bytes, *, now_ms: int) -> dict[str, Any]:
        return self.checkpoint.ingest(raw, now_ms=now_ms)

    def packet_for(self, outcome_id: str) -> dict[str, Any]:
        return self.checkpoint.packet_for(outcome_id)

    def checkpoint_status(self) -> dict[str, Any]:
        return self.checkpoint.status()

    def checkpoint_outcomes(self, *, now_ms: int) -> list[dict[str, Any]]:
        return self.checkpoint.outcomes(now_ms=now_ms)

    def close(self) -> None:
        self.checkpoint.close()

    def rebind_checkpoint_release(
        self,
        release: VerifiedRelease,
        *,
        authority_key: Ed25519PublicKey,
        floor: ReleaseFloor,
        now_ms: int,
    ) -> None:
        """Explicit signed forward release adoption; no source cursor or financial writes."""
        floor.require_active(release)
        self.checkpoint.rebind(release, authority_key, now_ms=now_ms)
        self.release = release


def consume_once(
    *,
    state_db: Path,
    inbox: Path,
    release: VerifiedRelease,
    source_key: Ed25519PublicKey,
    now_ms: int,
    wire_version: int = 3,
    checkpoint_pins: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not inbox.is_dir() or inbox.is_symlink() or inbox.stat().st_mode & 0o077:
        raise ValueError("V4_PROTECTED_INBOX_INVALID")
    receiver = CheckpointSourceReceiver(
        state_db,
        release=release,
        source_key=source_key,
        wire_version=wire_version,
        checkpoint_pins=checkpoint_pins,
    )
    count, scanned = 0, 0
    try:
        for path in sorted(inbox.iterdir()):
            if not PACKET_NAME.fullmatch(path.name):
                raise ValueError("V4_UNEXPECTED_PACKET_NAME")
            raw = _read_pinned(path)
            if sha256(raw).hexdigest()[:16] != path.name[-21:-5]:
                raise ValueError("V4_PACKET_NAME_DIGEST_DRIFT")
            result = receiver.ingest(raw, now_ms=now_ms)
            count += result["events"]
            scanned += 1
        return {
            "schema": "cwsa_protected_source_status_v3",
            "state": "SHADOW_SOURCE_ONLY_NO_ORDER_ROUTE",
            "processed_source_events": count,
            "packet_files_checked": scanned,
            "checkpoint_status": receiver.checkpoint_status(),
            "opening_candidates": sum(
                item["opening_candidate"] for item in receiver.checkpoint_outcomes(now_ms=now_ms)
            ),
            "order_route": "NONE",
            "trading_authorized": False,
        }
    finally:
        receiver.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--source-public", type=Path, required=True)
    parser.add_argument("--approver-public", type=Path, required=True)
    parser.add_argument("--release-floor", type=Path, required=True)
    parser.add_argument("--account-binding", required=True)
    parser.add_argument("--risk-policy-sha", required=True)
    parser.add_argument("--rulebook-sha", required=True)
    parser.add_argument("--wire-version", type=int, choices=(3,), default=3)
    parser.add_argument("--checkpoint-pins", type=Path)
    args = parser.parse_args(argv)
    try:
        now_ms = time.time_ns() // 1_000_000
        approver = Ed25519PublicKey.from_public_bytes(_read_pinned(args.approver_public))
        source = Ed25519PublicKey.from_public_bytes(_read_pinned(args.source_public))
        release_envelope = load_json(_read_pinned(args.release))
        with ReleaseFloor(args.release_floor) as floor:
            verified = ReleaseVerifier(
                approver,
                "approver",
                args.account_binding,
                args.risk_policy_sha,
                args.rulebook_sha,
            ).verify(release_envelope, now_ms=now_ms, floor=floor)
            floor.require_active(verified)
            result = consume_once(
                state_db=args.state_db,
                inbox=args.inbox,
                release=verified,
                source_key=source,
                now_ms=now_ms,
                wire_version=args.wire_version,
                checkpoint_pins=(
                    load_json(_read_pinned(args.checkpoint_pins)) if args.checkpoint_pins else None
                ),
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "schema": "cwsa_protected_source_status_v3",
                    "state": "BLOCKED",
                    "reason": type(exc).__name__,
                    "order_route": "NONE",
                    "trading_authorized": False,
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
