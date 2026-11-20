"""Read-only hash-chain and semantic replay of a closed Guardian journal copy."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import canonical, digest, load_json
from .financial_semantics import CONTRACT, validate_transition


@dataclass(frozen=True)
class ArchiveScan:
    sequence: int
    head: str
    legacy_events: int
    semantic_events: int
    release_shas: tuple[str, ...]


def scan_archive(path: Path) -> ArchiveScan:
    if path.is_symlink() or not path.is_file():
        raise ValueError("ARCHIVE_PATH_REQUIRED")
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("ARCHIVE_REQUIRES_CLOSED_CHECKPOINTED_COPY")
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("CORRUPT_DATABASE")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("FOREIGN_KEY_FAILURE")
        state: dict[str, Any] = {}
        sequence, legacy_events, semantic_events, head = 0, 0, 0, "0" * 64
        release_shas: set[str] = set()
        for seq, kind, at_ms, raw, prev, recorded in db.execute(
            "SELECT seq,kind,at_ms,payload,prev,sha FROM events ORDER BY seq"
        ):
            if seq != sequence + 1 or prev != head:
                raise ValueError("JOURNAL_CHAIN_GAP")
            payload = load_json(raw)
            expected = digest(
                {"seq": seq, "kind": kind, "at_ms": at_ms, "payload": payload, "prev": prev}
            )
            if expected != recorded or payload["sequence"] != seq:
                raise ValueError("JOURNAL_TAMPER")
            active = payload.get("active_release_sha")
            if isinstance(active, str):
                release_shas.add(active)
            if payload.get("_semantic_contract") == CONTRACT:
                validate_transition(state, payload, kind, previous_head=head)
                semantic_events += 1
            elif state.get("_semantic_contract") == CONTRACT:
                raise ValueError("JOURNAL_SEMANTIC_VERSION_DOWNGRADE")
            else:
                legacy_events += 1
            sequence, state, head = seq, payload, recorded
        snapshot = db.execute("SELECT seq,payload,sha FROM snapshot WHERE id=1").fetchone()
        if not sequence:
            raise ValueError("ORPHAN_SNAPSHOT" if snapshot is not None else "EMPTY_JOURNAL")
        if snapshot != (sequence, canonical(state).decode(), head):
            raise ValueError("SNAPSHOT_MISMATCH")
    return ArchiveScan(sequence, head, legacy_events, semantic_events, tuple(sorted(release_shas)))
