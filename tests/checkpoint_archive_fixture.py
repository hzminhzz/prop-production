"""Frozen synthetic SQL dumps and builders from before the Phase D cutover."""

import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from prop_production.evidence import canonical, digest

FIXED_K = "FIXED_K_NATIVE_QUANTITY"


def restore(root, fixture):
    path = root / (fixture + ".sqlite")
    compressed = Path(__file__).with_name("fixtures") / (fixture + ".sql.gz")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(gzip.decompress(compressed.read_bytes()).decode())
    return path


def v1_release_body(
    *,
    now_ms: int = 1_794_441_900_000,
    generation: int = 1,
    sizing_policy: str = FIXED_K,
    k: str = "0.04",
) -> dict[str, Any]:
    """The original portfolio_release_v1 payload shape: signed fields only, no mandates."""
    member: dict[str, Any] = {
        "pod_id": "pod-a",
        "source_identity": "fixture-leader",
        "identity_known_at_ms": 1,
        "account_scope": ["fixture-leader"],
        "allocation": "2000",
        "sizing_policy": sizing_policy,
        "allowed_markets": ["BTC", "ETH"],
    }
    if sizing_policy == FIXED_K:
        member["k"] = k
    payload: dict[str, Any] = {
        "schema": "portfolio_release_v1",
        "generation": generation,
        "account_binding": "fixture-account",
        "created_at_ms": now_ms - 2_000,
        "expires_at_ms": now_ms + 3_600_000,
        "selection_known_at_ms": now_ms - 3_000,
        "members": [member],
        "inventory_sha": digest("synthetic-inventory"),
        "decision_sha": digest("synthetic-decision"),
        "research_commit": "a" * 40,
        "risk_policy_sha": digest("synthetic-risk"),
        "rulebook_sha": digest("synthetic-rules"),
        "batch_seconds": 300,
        "source_mode": "SYNTHETIC",
    }
    payload["definition_sha"] = digest(payload["members"])
    payload["source_sha"] = digest(
        {
            name: payload[name]
            for name in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    return payload


def presemantic_archive(root: Path, *, release_sha: str, name: str = "presemantic") -> Path:
    """A closed pre-semantic chain: hash-only evidence, no ``_semantic_contract`` row."""
    path = root / (name + ".sqlite")
    head = ["0" * 64]
    rows: list[tuple[int, str, int, str, str, str]] = []

    def emit(kind: str, at_ms: int, payload: dict[str, Any]) -> None:
        seq = len(rows) + 1
        body = json.loads(
            canonical({"schema": "guardian_journal_v1", "account_binding": "fixture-account", "sequence": seq, **payload})
        )
        sha = digest({"seq": seq, "kind": kind, "at_ms": at_ms, "payload": body, "prev": head[0]})
        rows.append((seq, kind, at_ms, canonical(body).decode(), head[0], sha))
        head[0] = sha

    runtime = {"state": "DISARMED", "boot_epoch": 1, "broker_epoch": 1, "certainty": "UNKNOWN"}
    emit(
        "BOOT_DISARMED",
        0,
        {"decisions": {}, "finance": {}, "halts": [], "intents": {}, "runtime": runtime},
    )
    emit(
        "BIND_RELEASE_DISARMED",
        0,
        {
            "active_release_sha": release_sha,
            "decisions": {},
            "finance": {},
            "halts": [],
            "intents": {},
            "runtime": runtime,
        },
    )
    emit(
        "RECONCILE",
        301_000,
        {
            "active_release_sha": release_sha,
            "decisions": {},
            "finance": {
                "cash": "10000",
                "equity": "10000",
                "day": 0,
                "observed_at_ms": 301_000,
                "realized_made": "0",
                "unrealized": "0",
            },
            "halts": [],
            "intents": {},
            "runtime": {**runtime, "certainty": "COHERENT"},
        },
    )
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
            "at_ms INTEGER NOT NULL,payload TEXT NOT NULL,prev TEXT NOT NULL,sha TEXT NOT NULL);"
            "CREATE TABLE snapshot (id INTEGER PRIMARY KEY CHECK(id=1),"
            "seq INTEGER NOT NULL REFERENCES events(seq),payload TEXT NOT NULL,sha TEXT NOT NULL);"
        )
        db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", rows)
        db.execute("INSERT INTO snapshot VALUES (1,?,?,?)", (rows[-1][0], rows[-1][3], head[0]))
        db.commit()
    return path
