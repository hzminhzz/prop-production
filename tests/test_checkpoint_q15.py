"""Versioned Q15 receipts verified through actual signed source and financial replay."""

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from checkpoint_archive_fixture import restore
from copytrade_kernel import DECISION_SEMANTICS, Q15_REJECTION
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from test_checkpoint_receiver import KEY

from prop_production.checkpoint_admission import SEMANTICS_FIELD
from prop_production.checkpoint_fixture import START, packets
from prop_production.checkpoint_receiver import CheckpointSourceReceiver
from prop_production.evidence import canonical, load_json, sign
from prop_production.finance import Rulebook
from prop_production.guardian import Guardian
from prop_production.journal import Journal
from prop_production.judge.broker import FakeBroker
from prop_production.release import ReleaseFloor, ReleaseVerifier
from prop_production.worker import initialize_fixture, public_fixture_key


def test_pre_cutover_q15_receipt_replays_with_unchanged_bytes(tmp_path):
    path = restore(tmp_path, "checkpoint_q15_legacy_v2")
    data = path.read_bytes()
    state = Journal.read(path)
    assert path.read_bytes() == data
    assert SEMANTICS_FIELD not in state
    assert [Decimal(intent["quantity"]) for intent in state["intents"].values()] == [
        Decimal("0.025")
    ]
    for record in state["checkpoint_decisions"].values():
        for attempt in record["attempts"]:
            assert SEMANTICS_FIELD not in attempt
            assert attempt["projection_version"] == "checkpoint_execution_projection_v2"


@pytest.mark.parametrize(("retry", "expected"), [("4", "0.10000"), ("-1", "-0.02500")])
def test_q15_decrease_and_rebound_cannot_retry_but_increase_or_flip_can(tmp_path, retry, expected):
    root = tmp_path / "q15"
    authority, source = (public_fixture_key(17, purpose) for purpose in ("approver", "source"))
    initialize_fixture(root, expires_at_ms=2_000_000)
    settings = load_json((root / "trust/settings.json").read_bytes())
    rules = Rulebook(**settings["rules"])
    release = ReleaseVerifier(
        authority.public_key(),
        "approver",
        "fixture-account",
        settings["release"]["payload"]["risk_policy_sha"],
        rules.sha,
    ).verify(settings["release"], now_ms=1000)
    with (
        Journal(root / "guardian/account.sqlite", "fixture-account") as journal,
        ReleaseFloor(root / "trust/floor.sqlite") as floor,
    ):
        broker = FakeBroker(root / "broker/test.sqlite")
        receiver = CheckpointSourceReceiver(
            root / "guardian/cwsa.sqlite", release=release, source_key=source.public_key()
        )
        try:
            guardian = Guardian(
                journal,
                release,
                floor,
                source.public_key(),
                rules,
                settings["policy"],
                settings["markets"],
                broker,
                mandate_key=authority.public_key(),
            )
            mandate = release.payload["mandates"][0]
            guardian.activate_copy_mandate(
                sign(mandate, authority, "copy-mandate-v1"), now_ms=START - 1
            )
            owner = next(iter(journal.snapshot()["copy_mandates"]))

            def review(now, outcome):
                return guardian.review_checkpoints(
                    receiver,
                    {owner: outcome},
                    quotes={
                        "BTC": {"bid": "10000", "ask": "10000", "known_at_ms": now},
                    },
                    now_ms=now,
                )

            for index, packet in enumerate(packets(source, ("2", "3", "1", "2", "3", retry))):
                body = packet["payload"]
                now = body["issued_ms"]
                outcome = body["transitions"][-1]["effect"]["outcome_id"]
                receiver.ingest(canonical(packet), now_ms=now)
                if not index:
                    guardian.baseline_copy_mandate(owner, receiver, now_ms=now)
                    continue
                guardian.reconcile(now)
                result = review(now, outcome)
                if index == 1:
                    assert result["status"] == "REVIEWABLE"
                    assert review(result["deadline_ms"], outcome)["status"] == "MISSED"
                elif index in (2, 3, 4):
                    assert result["status"] == "MISSED"
                    assert result["reasons"] == [Q15_REJECTION]
                    assert not journal.snapshot()["intents"]
                else:
                    assert result["status"] == "REVIEWABLE"
                    assert Decimal(result["execution_plan"]["targets"]["BTC"]) == Decimal(expected)
                    first = guardian.reserve_checkpoint(
                        result["decision_id"], receiver=receiver, now_ms=now
                    )
                    assert len(first) == 1
                    assert (
                        guardian.reserve_checkpoint(
                            result["decision_id"], receiver=receiver, now_ms=now
                        )
                        == first
                    )
            state = journal.snapshot()
            assert state[SEMANTICS_FIELD] == DECISION_SEMANTICS
            assert all(
                a[SEMANTICS_FIELD] == DECISION_SEMANTICS
                for r in state["checkpoint_decisions"].values()
                for a in r["attempts"]
            )
            assert journal.verify()["integrity"] == "VERIFIED"
            assert broker.audit() == []
        finally:
            receiver.close()
            broker.close()


def test_forward_cutover_preserves_old_miss_and_rejects_downgrade(tmp_path):
    path = restore(tmp_path, "checkpoint_q15_legacy_v2")
    # Restore the exact, earlier chain prefix at the first MISSED receipt.
    with closing(sqlite3.connect(path)) as db, db:
        sequence, payload, head = next(
            (seq, raw, sha)
            for seq, raw, sha in db.execute(
                "SELECT seq,payload,sha FROM events WHERE kind='CHECKPOINT_REVIEW' ORDER BY seq"
            )
            if any(
                record["result"]["status"] == "MISSED"
                for record in load_json(raw)["checkpoint_decisions"].values()
            )
        )
        db.execute(
            "UPDATE snapshot SET seq=?,payload=?,sha=? WHERE id=1", (sequence, payload, head)
        )
        db.execute("DELETE FROM events WHERE seq>?", (sequence,))
    before = Journal.read(path)
    signed_release = before["checkpoint_release"]
    authority = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(signed_release["authority_public"])
    )
    envelope = signed_release["envelope"]
    body = envelope["payload"]
    owner, entry = next(iter(before["copy_mandates"].items()))
    policy, markets = (entry["risk_contract"][key] for key in ("policy", "markets"))
    source = KEY.public_key()
    rules = Rulebook.synthetic()
    release = ReleaseVerifier(
        authority, "approver", "fixture-account", body["risk_policy_sha"], rules.sha
    ).verify(
        envelope,
        now_ms=body["created_at_ms"],
    )
    vectors = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("cwsa_checkpoint_vectors_v3.json")
        .read_bytes()
    )
    with (
        Journal(path, "fixture-account") as journal,
        ReleaseFloor(tmp_path / "floor", initialize=True) as floor,
    ):
        floor.accept(release)
        broker = FakeBroker(tmp_path / "broker.sqlite")
        receiver = CheckpointSourceReceiver(
            tmp_path / "cwsa.sqlite", release=release, source_key=source
        )
        try:
            guardian = Guardian(
                journal,
                release,
                floor,
                source,
                rules,
                policy,
                markets,
                broker,
                mandate_key=authority,
            )
            base, missed, retry = vectors["sources"][0]["packets"][:3]
            for packet in (base, missed, retry):
                receiver.ingest(canonical(packet), now_ms=packet["payload"]["issued_ms"])
            now = retry["payload"]["issued_ms"]
            guardian.reconcile(now)
            outcome = retry["payload"]["transitions"][-1]["effect"]["outcome_id"]
            result = guardian.review_checkpoints(
                receiver,
                {owner: outcome},
                now_ms=now,
                quotes={
                    "BTC": {"bid": "10000", "ask": "10000", "known_at_ms": now},
                },
            )
            assert result["reasons"] == [Q15_REJECTION]
            state = journal.snapshot()
            assert state[SEMANTICS_FIELD] == DECISION_SEMANTICS
            for key, record in before["checkpoint_decisions"].items():
                assert state["checkpoint_decisions"][key] == record
            with pytest.raises(ValueError, match="CHECKPOINT_DECISION_SEMANTICS_TRANSITION"):
                journal.update("TEST_DOWNGRADE", now, lambda row: row.pop(SEMANTICS_FIELD))
            assert journal.verify()["integrity"] == "VERIFIED"
            assert not state["intents"] and not broker.audit()
        finally:
            receiver.close()
            broker.close()
