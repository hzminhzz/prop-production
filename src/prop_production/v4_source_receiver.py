"""Protected read-only receiver for externally signed V4 position packets.

Only immutable quant-owned source packets cross into this role. A root-mediated
file transfer must provision the protected inbox; neither the producer's
private signing key nor broker credentials are read by this process.

This receiver authorizes NO orders and does not substitute for Guardian's
financial admission. It records source continuity and T+300 eligibility.
"""

from __future__ import annotations

import argparse
import fcntl
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

from .checkpoint_source import (
    AUTHORITY as CHECKPOINT_AUTHORITY,
)
from .checkpoint_source import (
    GRID_MS,
    fields,
    integer,
    validate_pins,
    verify_transition,
)
from .checkpoint_source import (
    ZERO as CHECKPOINT_ZERO,
)
from .compatibility import load_manifest
from .contracts import (
    CHECKPOINT_SCHEMA_V3,
    PORTFOLIO_RELEASE_V2,
    V4_BINDING_V1,
    V4_BINDING_V2,
    V4_EXPORT_V1,
    V4_EXPORT_V2,
)
from .evidence import canonical, digest, load_json, require_sha, verify
from .release import (
    ReleaseFloor,
    ReleaseVerifier,
    VerifiedRelease,
    binding_sha,
    replay_release_binding,
    transition_release_binding,
)
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


def validated_batch(
    raw: bytes,
    key: Ed25519PublicKey,
    members: set[str],
) -> tuple[dict[str, Any], dict[str, str], dict[str, int]]:
    body = load_json(raw)
    if not isinstance(body, dict) or body.get("trading_authorized") is not False:
        raise ValueError("V4_BATCH_SCHEMA")
    v2 = body.get("schema") == V4_EXPORT_V2
    expected_keys = {"schema", "head_cursors", "events", "trading_authorized", "batch_envelope"} | (
        {"anchor"} if v2 else set()
    )
    if set(body) != expected_keys or body["schema"] not in (V4_EXPORT_V1, V4_EXPORT_V2):
        raise ValueError("V4_BATCH_SCHEMA")
    binding = verify(body["batch_envelope"], key, "source-observer-v4-batch")
    expected_binding_keys = {
        "schema",
        "selected",
        "head_cursors",
        "events_sha",
        "trading_authorized",
    } | ({"anchor_sha", "wire_epoch"} if v2 else set())
    if (
        set(binding) != expected_binding_keys
        or binding["schema"] != (V4_BINDING_V2 if v2 else V4_BINDING_V1)
        or binding["trading_authorized"] is not False
        or binding["head_cursors"] != body["head_cursors"]
        or binding["events_sha"] != digest(body["events"])
    ):
        raise ValueError("V4_BATCH_BINDING")
    if v2:
        anchor = verify(body["anchor"], key, "source-observer-v4-anchor")
        if (
            anchor.get("schema") != "v4_source_stream_head_anchor_v2"
            or anchor.get("selected") != binding.get("selected")
            or anchor.get("wire_epoch") != binding.get("wire_epoch")
            or type(anchor.get("cutoff_ms")) is not int
            or anchor["cutoff_ms"] <= 0
            or anchor.get("trading_authorized") is not False
            or not isinstance(anchor.get("heads"), dict)
            or binding.get("anchor_sha") != digest(anchor)
        ):
            raise ValueError("V4_V2_SIGNED_ANCHOR_INVALID")
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
    if v2 and (
        set(anchor["heads"]) != set(selected)
        or any(
            not isinstance(anchor["heads"][a], dict)
            or type(anchor["heads"][a].get("cursor")) is not int
            or not 0 <= anchor["heads"][a]["cursor"] <= heads[a]
            or not isinstance(anchor["heads"][a].get("chain_sha"), str)
            or re.fullmatch(r"[0-9a-f]{64}", anchor["heads"][a]["chain_sha"]) is None
            for a in selected
        )
    ):
        raise ValueError("V4_V2_ANCHOR_HEADS_INVALID")
    for alias, packets in body["events"].items():
        for event in packets:
            if not isinstance(event, dict) or set(event) != {"proof", "book"}:
                raise ValueError("V4_BATCH_EVENT_SHAPE")
            if not v2:
                # Preserve v1's exact historical assurance semantics. V2
                # authenticates the packet first, then scopes local faults to
                # their wallet stream after signature checking.
                proof = verify(event["proof"], key, "source-observer-v4")
                if (
                    proof["source_identity"] != selected[alias]
                    or proof["stream_id"] != "account:" + alias
                    or proof["stream_cursor"] > heads[alias]
                ):
                    raise ValueError("V4_SIGNED_BATCH_ALIAS_DRIFT")
    return body, selected, heads


def validate_source_packet(raw: bytes, source_key: Ed25519PublicKey) -> None:
    """Authenticate an incoming packet before root handoff regardless of wire version."""
    packet = load_json(raw)
    if packet.get("authority") == CHECKPOINT_SCHEMA_V3:
        proof = verify(packet, source_key, CHECKPOINT_SCHEMA_V3)
        if proof["schema"] != "cwsa_checkpoint_packet_v3" or proof["trading_authorized"] is not False:
            raise ValueError("CWSA_HANDOFF_SCHEMA")
        validate_pins(
            {"policy": proof["policy"], "scopes": {proof["scope"]["source_id"]: proof["scope"]}}
        )
        for raw_sha, original in proof["originals"].items():
            if sha256(original.encode()).hexdigest() != raw_sha:
                raise ValueError("CWSA_HANDOFF_ORIGINAL_HASH")
        for event in proof["transitions"]:
            verify_transition(
                proof["scope"],
                proof["policy"],
                event,
                proof["originals"],
                issued_ms=proof["issued_ms"],
            )
        return
    proof = verify(packet["batch_envelope"], source_key, "source-observer-v4-batch")
    validated_batch(raw, source_key, set(proof["selected"].values()))


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


class SourcePacketReceiver:
    """One private SQLite writer: signed source events + read-only eligibility."""

    def __init__(
        self,
        path: Path,
        *,
        release: VerifiedRelease,
        source_key: Ed25519PublicKey,
        wire_version: int = 1,
        quarantine_key: Ed25519PublicKey | None = None,
        checkpoint_pins: dict[str, Any] | None = None,
    ):
        if (
            quarantine_key is not None
            and quarantine_key.public_bytes_raw() == source_key.public_bytes_raw()
        ):
            raise ValueError("V4_QUARANTINE_AUTHORITY_NOT_SEPARATE")
        self.quarantine_key = quarantine_key
        if wire_version not in (1, 2, 3):
            raise ValueError("V4_RECEIVER_WIRE_VERSION_INVALID")
        supported = load_manifest()["contracts"].get("source_wire_versions", [1])
        if wire_version not in supported:
            raise ValueError("V4_WIRE_NOT_PINNED_BY_COMPATIBILITY")
        self.path = path
        self.release = release
        self.source_key = source_key
        self.wire_version = wire_version
        self.projector: V4ShadowProjection | None = None
        self.checkpoint: CheckpointProjection | None = None
        if wire_version == 3:
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
            return
        if path.exists():
            with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as db:
                existing = db.execute("SELECT cutoff_ms FROM config WHERE id=1").fetchone()
                fields = {row[1] for row in db.execute("PRAGMA table_info(bridge_meta)")}
                if wire_version == 2:
                    if not {"wire_version", "anchor_sha", "wire_epoch"}.issubset(fields):
                        raise ValueError("V4_V2_RECEIVER_META_MISSING")
                    meta = db.execute(
                        "SELECT wire_version,anchor_sha,wire_epoch FROM bridge_meta WHERE id=1"
                    ).fetchone()
                    if meta is None or meta[0] != wire_version:
                        raise ValueError("V4_WIRE_DOWNGRADE_OR_DRIFT")
                else:
                    meta = (1, None, None)
                    if "wire_version" in fields:
                        row = db.execute(
                            "SELECT wire_version FROM bridge_meta WHERE id=1"
                        ).fetchone()
                        if row is None or row[0] != 1:
                            raise ValueError("V4_WIRE_DOWNGRADE_OR_DRIFT")
            if existing is None:
                raise ValueError("V4_RECEIVER_INCOMPLETE_STATE")
            self.projector = V4ShadowProjection(
                path,
                release=release,
                source_key=source_key,
                cutoff_ms=existing[0],
                anchor_sha=meta[1],
                wire_epoch=meta[2],
            )
            self._verify_meta()

    def rebind_checkpoint_release(
        self,
        release: VerifiedRelease,
        *,
        authority_key: Ed25519PublicKey,
        floor: ReleaseFloor,
        now_ms: int,
    ) -> None:
        """Explicit signed forward release adoption; no source cursor or financial writes."""
        if self.wire_version != 3 or self.checkpoint is None:
            raise ValueError("CWSA_RELEASE_REBIND_WIRE")
        floor.require_active(release)
        self.checkpoint.rebind(release, authority_key, now_ms=now_ms)
        self.release = release

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

    def _bootstrap(
        self,
        selected: dict[str, str],
        heads: dict[str, int],
        *,
        now_ms: int,
        anchor: dict[str, Any] | None = None,
    ) -> None:
        if self.projector is not None:
            raise ValueError("V4_RECEIVER_ALREADY_BOOTSTRAPPED")
        if self.wire_version == 2:
            if anchor is None or anchor["cutoff_ms"] > now_ms:
                raise ValueError("V4_V2_SIGNED_CUTOFF_FUTURE_OR_MISSING")
            cursors = {
                identity: anchor["heads"][alias]["cursor"] for alias, identity in selected.items()
            }
            chains = {
                identity: anchor["heads"][alias]["chain_sha"]
                for alias, identity in selected.items()
            }
        else:
            cursors = {identity: heads[alias] for alias, identity in selected.items()}
            chains = None
        self.projector = V4ShadowProjection(
            self.path,
            release=self.release,
            source_key=self.source_key,
            cutoff_ms=anchor["cutoff_ms"] if anchor is not None else now_ms,
            initial_cursors=cursors,
            initial_chains=chains,
            anchor_sha=digest(anchor) if anchor is not None else None,
            wire_epoch=anchor["wire_epoch"] if anchor is not None else None,
        )
        con = self.projector.con
        if self.wire_version == 2:
            con.executescript(
                "CREATE TABLE bridge_meta("
                "id INTEGER PRIMARY KEY CHECK(id=1), selection_sha TEXT NOT NULL,"
                "source_public_sha TEXT NOT NULL,release_sha TEXT NOT NULL,"
                "wire_version INTEGER NOT NULL,anchor_sha TEXT NOT NULL,wire_epoch TEXT NOT NULL);"
                "CREATE TABLE bridge_packets("
                "packet_sha TEXT PRIMARY KEY,processed_at_ms INTEGER NOT NULL);"
                "CREATE TABLE bridge_packet_status("
                "packet_sha TEXT PRIMARY KEY,status TEXT NOT NULL,"
                "original_events INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL);"
                "CREATE TABLE bridge_packet_events("
                "packet_sha TEXT NOT NULL,alias TEXT NOT NULL,event_index INTEGER NOT NULL,"
                "event_sha TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,"
                "PRIMARY KEY(packet_sha,alias,event_index));"
                "CREATE TABLE bridge_quarantine_resolutions("
                "nonce TEXT PRIMARY KEY,packet_sha TEXT NOT NULL,alias TEXT NOT NULL,"
                "receipt_sha TEXT NOT NULL,baseline_sha TEXT NOT NULL,"
                "resolved_at_ms INTEGER NOT NULL);"
            )
            con.execute(
                "INSERT INTO bridge_meta VALUES(1,?,?,?,?,?,?)",
                (
                    digest(selected),
                    sha256(self.source_key.public_bytes_raw()).hexdigest(),
                    self.release.release_sha,
                    2,
                    self.projector.anchor_sha,
                    self.projector.wire_epoch,
                ),
            )
        else:
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
        if self.checkpoint is not None:
            return self.checkpoint.ingest(raw, now_ms=now_ms)
        members = {m["source_identity"] for m in self.release.payload["members"]}
        packet, selected, heads = validated_batch(raw, self.source_key, members)
        incoming_wire = 2 if packet["schema"] == V4_EXPORT_V2 else 1
        if incoming_wire != self.wire_version:
            raise ValueError("V4_SOURCE_WIRE_DOWNGRADE_OR_MIXED_VERSION")
        anchor = (
            verify(packet["anchor"], self.source_key, "source-observer-v4-anchor")
            if self.wire_version == 2
            else None
        )
        first = self.projector is None
        if first and any(packet["events"].values()):
            raise ValueError("V4_FIRST_BATCH_MUST_BE_BASELINE")
        if first:
            self._bootstrap(selected, heads, now_ms=now_ms, anchor=anchor)
        assert self.projector is not None
        if anchor is not None and (
            self.projector.anchor_sha != digest(anchor)
            or self.projector.wire_epoch != anchor["wire_epoch"]
        ):
            raise ValueError("V4_V2_ANCHOR_OR_EPOCH_DRIFT")
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
                "schema": "v4_protected_receiver_v1"
                if self.wire_version == 1
                else "v4_protected_receiver_v2",
                "state": "DUPLICATE",
                "events": 0,
                "trading_authorized": False,
            }
        if self.wire_version == 2:
            return self._ingest_v2(packet, packet_sha=packet_sha, now_ms=now_ms)
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

    def _ingest_v2(
        self,
        packet: dict[str, Any],
        *,
        packet_sha: str,
        now_ms: int,
    ) -> dict[str, Any]:
        """Commit wallet-local progress with its source projection, not globally.

        A complete signed envelope authenticates all packet bytes, while an
        invalid attributed wallet event is persisted as a durable quarantine.
        It cannot be silently retried as an accepted event or marked COMPLETE.
        """
        assert self.projector is not None
        con = self.projector.con
        total = sum(len(items) for items in packet["events"].values())
        status_row = con.execute(
            "SELECT status FROM bridge_packet_status WHERE packet_sha=?", (packet_sha,)
        ).fetchone()
        if status_row is not None and status_row[0] == "RESOLVED_WITH_QUARANTINE":
            return {
                "schema": "v4_protected_receiver_v2",
                "state": "RESOLVED_WITH_QUARANTINE",
                "events": 0,
                "quarantined_events": 0,
                "original_events": total,
                "trading_authorized": False,
            }
        count, quarantined = 0, 0
        signed_selected = verify(
            packet["batch_envelope"],
            self.source_key,
            "source-observer-v4-batch",
        )["selected"]
        for alias, records in sorted(packet["events"].items()):
            for index, record in enumerate(records):
                event_sha = digest(record)
                row = con.execute(
                    "SELECT event_sha,status FROM bridge_packet_events "
                    "WHERE packet_sha=? AND alias=? AND event_index=?",
                    (packet_sha, alias, index),
                ).fetchone()
                if row is not None:
                    if row[0] != event_sha:
                        raise ValueError("V4_V2_PACKET_PROGRESS_EQUIVOCATION")
                    if row[1] == "ACCEPTED":
                        continue
                    if row[1] == "QUARANTINED":
                        quarantined += 1
                        continue
                    raise ValueError("V4_V2_PACKET_PROGRESS_STATE_INVALID")

                def record_acceptance(db: sqlite3.Connection) -> None:
                    db.execute(
                        "INSERT INTO bridge_packet_events VALUES (?,?,?,?,?,?)",
                        (packet_sha, alias, index, event_sha, "ACCEPTED", None),
                    )

                try:
                    proof = verify(record["proof"], self.source_key, "source-observer-v4")
                    if (
                        proof.get("source_identity") != signed_selected[alias]
                        or proof.get("stream_id") != "account:" + alias
                        or proof.get("stream_cursor") > packet["head_cursors"][alias]
                    ):
                        raise ValueError("V4_V2_WALLET_ATTRIBUTION_MISMATCH")
                    self.projector.ingest(record, now_ms=now_ms, on_commit=record_acceptance)
                except (ValueError, KeyError, TypeError) as exc:
                    # Only failures in one authenticated wallet stream are
                    # quarantined; shared envelope/signature errors were
                    # already rejected before any source cursor mutation.
                    con.execute(
                        "INSERT INTO bridge_packet_events VALUES (?,?,?,?,?,?)",
                        (packet_sha, alias, index, event_sha, "QUARANTINED", str(exc)[:160]),
                    )
                    quarantined += 1
                else:
                    count += 1
        accepted = con.execute(
            "SELECT COUNT(*) FROM bridge_packet_events WHERE packet_sha=? AND status='ACCEPTED'",
            (packet_sha,),
        ).fetchone()[0]
        state = "COMPLETE" if accepted == total and quarantined == 0 else "PARTIAL"
        con.execute(
            "INSERT INTO bridge_packet_status VALUES (?,?,?,?) "
            "ON CONFLICT(packet_sha) DO UPDATE SET "
            "status=excluded.status,updated_at_ms=excluded.updated_at_ms",
            (packet_sha, state, total, now_ms),
        )
        if state == "COMPLETE":
            con.execute("INSERT INTO bridge_packets VALUES (?,?)", (packet_sha, now_ms))
        return {
            "schema": "v4_protected_receiver_v2",
            "state": state,
            "events": count,
            "quarantined_events": quarantined,
            "original_events": total,
            "trading_authorized": False,
        }

    def resolve_quarantine(
        self,
        *,
        packet_sha: str,
        alias: str,
        baseline: dict[str, Any],
        disposition: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        """Audited v2 recovery requires an authentic non-signaling source baseline.

        Neither the original rejected event nor its economic intent is accepted.
        The protected signer authenticates the request; the independent source
        signer and live SQLite predecessor prove whether remediation happened.
        """
        if self.wire_version != 2 or self.quarantine_key is None or self.projector is None:
            raise ValueError("V4_QUARANTINE_RECOVERY_AUTHORITY_NOT_AVAILABLE")
        if not isinstance(packet_sha, str) or re.fullmatch(r"[0-9a-f]{64}", packet_sha) is None:
            raise ValueError("V4_QUARANTINE_PACKET_INVALID")
        receipt = verify(disposition, self.quarantine_key, "v4-quarantine-operator")
        proof = verify(baseline["proof"], self.source_key, "source-observer-v4")
        if (
            receipt.get("schema") != "v4_wallet_quarantine_resolution_v1"
            or receipt.get("packet_sha") != packet_sha
            or receipt.get("alias") != alias
            or receipt.get("source_identity") != proof.get("source_identity")
            or receipt.get("account_binding") != self.release.payload["account_binding"]
            or receipt.get("release_sha") != self.release.release_sha
            or receipt.get("anchor_sha") != self.projector.anchor_sha
            or receipt.get("baseline_sha") != digest(baseline)
            or receipt.get("expected_stream_head") != proof.get("previous_stream_chain_sha")
            or receipt.get("reason_code") != "SOURCE_EVENT_QUARANTINED"
            or receipt.get("role") != "source_quarantine_operator"
            or type(receipt.get("issued_at_ms")) is not int
            or type(receipt.get("expires_at_ms")) is not int
            or not receipt["issued_at_ms"] <= now_ms < receipt["expires_at_ms"]
            or receipt["expires_at_ms"] - receipt["issued_at_ms"] > 900_000
            or not isinstance(receipt.get("nonce"), str)
            or re.fullmatch(r"[0-9a-f]{32}", receipt["nonce"]) is None
            or proof.get("kind") != "BASELINE"
            or proof.get("continuity") is not True
            or baseline.get("book") is None
        ):
            raise ValueError("V4_QUARANTINE_RECOVERY_PROOF_INVALID")
        chosen = self.projector.con.execute(
            "SELECT selection_sha FROM bridge_meta WHERE id=1"
        ).fetchone()[0]
        if chosen != digest(
            {
                member["pod_id"]: member["source_identity"]
                for member in self.release.payload["members"]
            }
        ):
            raise ValueError("V4_QUARANTINE_SELECTED_SCOPE_DRIFT")

        def certify(con: sqlite3.Connection) -> None:
            # Runs after the source cursor/predecessor proof, before COMMIT.
            matched = con.execute(
                "SELECT event_sha FROM bridge_packet_events "
                "WHERE packet_sha=? AND alias=? AND status='QUARANTINED'",
                (packet_sha, alias),
            ).fetchall()
            packet = con.execute(
                "SELECT status,original_events FROM bridge_packet_status WHERE packet_sha=?",
                (packet_sha,),
            ).fetchone()
            if (
                packet is None
                or packet[0] != "PARTIAL"
                or len(matched) != 1
                or receipt.get("quarantined_event_sha") != matched[0][0]
                or con.execute(
                    "SELECT COUNT(*) FROM bridge_packet_events WHERE packet_sha=?",
                    (packet_sha,),
                ).fetchone()[0]
                != packet[1]
                or con.execute(
                    "SELECT COUNT(*) FROM bridge_packet_events "
                    "WHERE packet_sha=? AND status='QUARANTINED'",
                    (packet_sha,),
                ).fetchone()[0]
                != 1
            ):
                raise ValueError("V4_QUARANTINE_UNRESOLVED_PEERS_OR_EVENT_DRIFT")
            # No stale pending increases from the quarantined source may survive.
            con.execute(
                "UPDATE markets SET pending_qty=NULL,pending_since_ms=NULL,"
                "pending_epoch=NULL WHERE identity=?",
                (proof["source_identity"],),
            )
            con.execute(
                "INSERT INTO bridge_quarantine_resolutions VALUES (?,?,?,?,?,?)",
                (receipt["nonce"], packet_sha, alias, digest(receipt), digest(baseline), now_ms),
            )
            con.execute(
                "UPDATE bridge_packet_status SET status='RESOLVED_WITH_QUARANTINE',"
                "updated_at_ms=? WHERE packet_sha=?",
                (now_ms, packet_sha),
            )

        actions = self.projector.ingest(
            baseline, now_ms=now_ms, on_commit=certify, require_new=True
        )
        if actions:
            raise ValueError("V4_QUARANTINE_BASELINE_CREATED_RISK")
        return {
            "schema": "v4_protected_receiver_v2",
            "state": "RESOLVED_WITH_QUARANTINE",
            "packet_sha": packet_sha,
            "trading_authorized": False,
        }

    def due(self, *, now_ms: int) -> list[dict[str, Any]]:
        if self.projector is None:
            return []
        return self.projector.due(now_ms)

    def checkpoint_status(self) -> dict[str, Any]:
        if self.checkpoint is None:
            raise ValueError("CWSA_RECEIVER_VERSION_REQUIRED")
        return self.checkpoint.status()

    def checkpoint_outcomes(self, *, now_ms: int) -> list[dict[str, Any]]:
        if self.checkpoint is None:
            raise ValueError("CWSA_RECEIVER_VERSION_REQUIRED")
        return self.checkpoint.outcomes(now_ms=now_ms)

    def close(self) -> None:
        if self.checkpoint is not None:
            self.checkpoint.close()
        if self.projector is not None:
            self.projector.close()


def consume_once(
    *,
    state_db: Path,
    inbox: Path,
    release: VerifiedRelease,
    source_key: Ed25519PublicKey,
    now_ms: int,
    wire_version: int = 1,
    checkpoint_pins: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not inbox.is_dir() or inbox.is_symlink() or inbox.stat().st_mode & 0o077:
        raise ValueError("V4_PROTECTED_INBOX_INVALID")
    receiver = SourcePacketReceiver(
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
        due = receiver.due(now_ms=now_ms)
        if wire_version == 3:
            return {
                "schema": "cwsa_protected_source_status_v3",
                "state": "SHADOW_SOURCE_ONLY_NO_ORDER_ROUTE",
                "processed_source_events": count,
                "packet_files_checked": scanned,
                "checkpoint_status": receiver.checkpoint_status(),
                "opening_candidates": sum(
                    item["opening_candidate"]
                    for item in receiver.checkpoint_outcomes(now_ms=now_ms)
                ),
                "order_route": "NONE",
                "trading_authorized": False,
            }
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
    parser.add_argument("--wire-version", type=int, choices=(1, 2, 3), default=1)
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
