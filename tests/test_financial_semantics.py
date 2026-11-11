"""Adversarial hash-valid journal semantics and append-only legacy checkpoints."""

import json
import sqlite3
from contextlib import closing

import pytest

from prop_production.evidence import canonical, digest
from prop_production.journal import Journal, StorageUnavailable, initial_state
from prop_production.judge.broker import FakeBroker


def test_hash_valid_forged_financial_transition_fails_semantic_replay(tmp_path):
    path = tmp_path / "financial.sqlite"
    with Journal(path, "fixture-account", initialize=True) as journal:
        before = journal.verify()
        assert before["semantic_status"] == "VERIFIED_V2"
        journal.halt("MANUAL", 100)
        final = journal.verify()

    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT seq,kind,at_ms,payload,prev FROM events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    seq, kind, at_ms, payload, prev = row
    body = json.loads(payload)
    body["positions"]["BTC"] = "999"  # no independent execution exists
    body["finance"]["equity"] = "100000000"
    row_hash = digest({"seq": seq, "kind": kind, "at_ms": at_ms, "payload": body, "prev": prev})
    con.execute(
        "UPDATE events SET payload=?,sha=? WHERE seq=?", (canonical(body).decode(), row_hash, seq)
    )
    con.execute(
        "UPDATE snapshot SET payload=?,sha=? WHERE id=1", (canonical(body).decode(), row_hash)
    )
    con.commit()
    con.close()
    with pytest.raises(StorageUnavailable) as error:
        Journal(path, "fixture-account")
    assert error.value.args == ("HALT_STORAGE",)
    assert final["head"] != row_hash  # chain SHA was recomputed correctly; legality failed


def test_illegal_semantics_fails_before_financial_commit(tmp_path):
    with Journal(tmp_path / "financial.sqlite", "fixture-account", initialize=True) as journal:
        previous = journal.sequence
        with pytest.raises(ValueError, match="ORPHAN_OR_REBOUND_FILL"):
            journal.update(
                "FORGED_FILL",
                100,
                lambda state: state["fills"].update(
                    {
                        "fake-execution": {
                            "execution_id": "fake-execution",
                            "intent_id": "absent",
                            "market": "BTC",
                            "quantity": "1",
                            "price": "100",
                            "fee": "0",
                            "known_at_ms": 100,
                        }
                    }
                ),
            )
        assert journal.sequence == previous
        assert journal.verify()["semantic_status"] == "VERIFIED_V2"


@pytest.mark.parametrize("kind", ["RECONCILE", "RECOVER_AUTH_MANUAL"])
def test_protected_halt_cannot_be_cleared_by_generic_or_forged_journal_commit(tmp_path, kind):
    """A valid journal SHA must not convert an ordinary update into recovery."""
    with Journal(tmp_path / "protected.sqlite", "protected-account", initialize=True) as journal:
        journal.halt("MANUAL", 100)
        sequence, head = journal.sequence, journal.head
        with pytest.raises(ValueError, match="GUARDIAN_SEMANTIC_UNAUTHORIZED_HALT_CLEARANCE"):
            journal.update(kind, 101, lambda state: state["halts"].clear())
        assert journal.snapshot()["halts"] == ["MANUAL"]
        assert (journal.sequence, journal.head) == (sequence, head)
        assert journal.verify()["integrity"] == "VERIFIED"


def insert_legacy_genesis(path):
    # Controlled fixture: old hash-chained bytes exactly as a v1 deployment
    # would have written, not a migration that rewrites real financial history.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT, at_ms INTEGER,"
            "payload TEXT, prev TEXT, sha TEXT)"
        )
        db.execute(
            "CREATE TABLE snapshot (id INTEGER PRIMARY KEY,seq INTEGER,payload TEXT,sha TEXT)"
        )
        body = initial_state("fixture-account")
        del body["_semantic_contract"]
        body["sequence"] = 1
        row_sha = digest(
            {
                "seq": 1,
                "kind": "BOOT_DISARMED",
                "at_ms": 0,
                "payload": body,
                "prev": "0" * 64,
            }
        )
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (1, "BOOT_DISARMED", 0, canonical(body).decode(), "0" * 64, row_sha),
        )
        db.execute(
            "INSERT INTO snapshot VALUES (1,?,?,?)",
            (1, canonical(body).decode(), row_sha),
        )
    return row_sha


def test_legacy_v1_is_preserved_and_checkpoints_only_after_provider_proof(tmp_path):
    path = tmp_path / "legacy.sqlite"
    old_head = insert_legacy_genesis(path)
    with Journal(path, "fixture-account") as journal:
        assert journal.verify()["semantic_status"] == "LEGACY_UNVERIFIABLE_V1"
        assert journal.con.execute("SELECT sha FROM events WHERE seq=1").fetchone()[0] == old_head
        with pytest.raises(ValueError, match="LEGACY_FINANCE"):
            journal.update("RESERVE_AND_OUTBOX", 1, lambda state: None)
        broker = FakeBroker(tmp_path / "broker.sqlite")
        evidence = broker.observe(301000)
        with pytest.raises(ValueError, match="RECONCILIATION"):
            journal.append_semantic_checkpoint(
                gateway=broker,
                now_ms=301000,
                expected_head=journal.head,
            )

        # The legacy source of financial truth is still structurally intact:
        # a coherent exact provider snapshot is recorded before checkpoint.
        def mark_verified(state):
            state["runtime"]["certainty"] = "COHERENT"
            state["runtime"]["provider_cutoff"] = evidence.cutoff
            state["runtime"]["last_snapshot_ms"] = evidence.observed_at_ms
            state["finance"]["cash"] = str(evidence.cash)

        journal.update("RECONCILE", 301000, mark_verified)
        cutoff_head = journal.head
        journal.append_semantic_checkpoint(
            gateway=broker,
            now_ms=301001,
            expected_head=cutoff_head,
        )
        assert journal.verify()["semantic_status"] == "FORWARD_V2_LEGACY_HISTORY_PRESERVED"
        assert journal.con.execute("SELECT sha FROM events WHERE seq=1").fetchone()[0] == old_head
        assert journal.snapshot()["runtime"]["state"] == "DISARMED"
        assert journal.snapshot()["runtime"]["permit"] is None
        broker.close()
