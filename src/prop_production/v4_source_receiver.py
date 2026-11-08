"""Protected read-only receiver for externally signed V4 position packets.

Only immutable quant-owned source packets cross into this role. A root-mediated
file transfer must provision the protected inbox; neither the producer's
private signing key nor broker credentials are read by this process.

This receiver authorizes NO orders and does not substitute for Guardian's
financial admission. It records source continuity and T+300 eligibility.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import digest, load_json, verify
from .release import ReleaseFloor, ReleaseVerifier, VerifiedRelease
from .v4_timing import V4ShadowProjection

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


def _validated_batch(
    raw: bytes,
    key: Ed25519PublicKey,
    members: set[str],
) -> tuple[dict[str, Any], dict[str, str], dict[str, int]]:
    body = load_json(raw)
    if (
        not isinstance(body, dict)
        or set(body) != {"schema", "head_cursors", "events", "trading_authorized", "batch_envelope"}
        or body["schema"] != "v4_signed_source_export_v1"
        or body["trading_authorized"] is not False
    ):
        raise ValueError("V4_BATCH_SCHEMA")
    binding = verify(body["batch_envelope"], key, "source-observer-v4-batch")
    if (
        set(binding) != {"schema", "selected", "head_cursors", "events_sha", "trading_authorized"}
        or binding["schema"] != "v4_source_batch_binding_v1"
        or binding["trading_authorized"] is not False
        or binding["head_cursors"] != body["head_cursors"]
        or binding["events_sha"] != digest(body["events"])
    ):
        raise ValueError("V4_BATCH_BINDING")
    selected = binding["selected"]
    if (
        not isinstance(selected, dict)
        or not selected
        or set(selected.values()) != members
        or len(set(selected.values())) != len(selected)
        or any(
            not isinstance(alias, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,70}", alias) is None
            or not isinstance(identity, str)
            or re.fullmatch(r"0x[a-f0-9]{40}", identity) is None
            for alias, identity in selected.items()
        )
    ):
        raise ValueError("V4_BATCH_RELEASE_SCOPE")
    heads = body["head_cursors"]
    if (
        not isinstance(heads, dict)
        or set(heads) != set(selected)
        or not isinstance(body["events"], dict)
        or set(body["events"]) != set(selected)
        or any(type(heads[a]) is not int or heads[a] < 1 for a in selected)
        or any(
            not isinstance(body["events"][a], list) or len(body["events"][a]) > 256
            for a in selected
        )
    ):
        raise ValueError("V4_BATCH_HEAD_SCHEMA")
    for alias, packets in body["events"].items():
        for event in packets:
            proof = verify(event["proof"], key, "source-observer-v4")
            if (
                proof["source_identity"] != selected[alias]
                or proof["stream_id"] != "account:" + alias
                or proof["stream_cursor"] > heads[alias]
            ):
                raise ValueError("V4_SIGNED_BATCH_ALIAS_DRIFT")
    return body, selected, heads


class SourcePacketReceiver:
    """One private SQLite writer: signed source events + read-only eligibility."""

    def __init__(
        self,
        path: Path,
        *,
        release: VerifiedRelease,
        source_key: Ed25519PublicKey,
    ):
        self.path = path
        self.release = release
        self.source_key = source_key
        self.projector: V4ShadowProjection | None = None
        if path.exists():
            with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as db:
                existing = db.execute("SELECT cutoff_ms FROM config WHERE id=1").fetchone()
            if existing is None:
                raise ValueError("V4_RECEIVER_INCOMPLETE_STATE")
            self.projector = V4ShadowProjection(
                path,
                release=release,
                source_key=source_key,
                cutoff_ms=existing[0],
            )
            self._verify_meta()

    def _verify_meta(self) -> None:
        assert self.projector is not None
        self.projector.con.executescript(
            "CREATE TABLE IF NOT EXISTS bridge_meta("
            "id INTEGER PRIMARY KEY CHECK(id=1), selection_sha TEXT NOT NULL,"
            "source_public_sha TEXT NOT NULL,release_sha TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS bridge_packets("
            "packet_sha TEXT PRIMARY KEY,processed_at_ms INTEGER NOT NULL);"
        )
        row = self.projector.con.execute(
            "SELECT selection_sha,source_public_sha,release_sha FROM bridge_meta WHERE id=1"
        ).fetchone()
        if row is None:
            raise ValueError("V4_RECEIVER_TRUST_META_MISSING")
        if (
            row[1] != sha256(self.source_key.public_bytes_raw()).hexdigest()
            or row[2] != self.release.release_sha
        ):
            raise ValueError("V4_RECEIVER_TRUST_DRIFT")

    def _bootstrap(self, selected: dict[str, str], heads: dict[str, int], *, now_ms: int) -> None:
        if self.projector is not None:
            raise ValueError("V4_RECEIVER_ALREADY_BOOTSTRAPPED")
        self.projector = V4ShadowProjection(
            self.path,
            release=self.release,
            source_key=self.source_key,
            cutoff_ms=now_ms,
            initial_cursors={identity: heads[alias] for alias, identity in selected.items()},
        )
        con = self.projector.con
        con.executescript(
            "CREATE TABLE bridge_meta("
            "id INTEGER PRIMARY KEY CHECK(id=1), selection_sha TEXT NOT NULL,"
            "source_public_sha TEXT NOT NULL,release_sha TEXT NOT NULL);"
            "CREATE TABLE bridge_packets("
            "packet_sha TEXT PRIMARY KEY,processed_at_ms INTEGER NOT NULL);"
        )
        con.execute(
            "INSERT INTO bridge_meta VALUES(1,?,?,?)",
            (
                digest(selected),
                sha256(self.source_key.public_bytes_raw()).hexdigest(),
                self.release.release_sha,
            ),
        )

    def ingest(self, raw: bytes, *, now_ms: int) -> dict[str, Any]:
        members = {m["source_identity"] for m in self.release.payload["members"]}
        packet, selected, heads = _validated_batch(raw, self.source_key, members)
        first = self.projector is None
        if first and any(packet["events"].values()):
            raise ValueError("V4_FIRST_BATCH_MUST_BE_BASELINE")
        if first:
            self._bootstrap(selected, heads, now_ms=now_ms)
        assert self.projector is not None
        chosen_sha = self.projector.con.execute(
            "SELECT selection_sha FROM bridge_meta WHERE id=1"
        ).fetchone()[0]
        if chosen_sha != digest(selected):
            raise ValueError("V4_BATCH_SELECTION_DRIFT")
        packet_sha = sha256(raw).hexdigest()
        seen = self.projector.con.execute(
            "SELECT 1 FROM bridge_packets WHERE packet_sha=?", (packet_sha,)
        ).fetchone()
        if seen:
            return {
                "schema": "v4_protected_receiver_v1",
                "state": "DUPLICATE",
                "events": 0,
                "trading_authorized": False,
            }
        count = 0
        for alias, events in packet["events"].items():
            for event in events:
                self.projector.ingest(event, now_ms=now_ms)
                count += 1
        self.projector.con.execute("INSERT INTO bridge_packets VALUES (?,?)", (packet_sha, now_ms))
        return {
            "schema": "v4_protected_receiver_v1",
            "state": "EVIDENCE_COLLECTED",
            "events": count,
            "trading_authorized": False,
        }

    def due(self, *, now_ms: int) -> list[dict[str, Any]]:
        if self.projector is None:
            return []
        return self.projector.due(now_ms)

    def close(self) -> None:
        if self.projector is not None:
            self.projector.close()


def consume_once(
    *,
    state_db: Path,
    inbox: Path,
    release: VerifiedRelease,
    source_key: Ed25519PublicKey,
    now_ms: int,
) -> dict[str, Any]:
    if not inbox.is_dir() or inbox.is_symlink() or inbox.stat().st_mode & 0o077:
        raise ValueError("V4_PROTECTED_INBOX_INVALID")
    receiver = SourcePacketReceiver(state_db, release=release, source_key=source_key)
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
        due = receiver.due(now_ms=now_ms)
        return {
            "schema": "v4_protected_source_status_v1",
            "state": "SHADOW_SOURCE_ONLY_NO_ORDER_ROUTE",
            "processed_source_events": count,
            "packet_files_checked": scanned,
            "due_shadow_eligibility": len(due),
            "guardian_financial_admission": "BLOCKED_NOT_WIRED",
            "independent_follower_quotes": "UNKNOWN",
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
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "schema": "v4_protected_source_status_v1",
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
