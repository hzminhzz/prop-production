"""Adversarial timing checks for externally signed official V4 position evidence."""

from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prop_production.evidence import digest, sign
from prop_production.release import ReleaseVerifier, fixture_release
from prop_production.v4_timing import V4ShadowProjection

IDENTITY = "0x" + "a" * 40
MARKET = "hyperliquid/mainnet:perp:default/BTC"


def setup(tmp_path, *, cutoff=100, cursor=0):
    approver = Ed25519PrivateKey.generate()
    source = Ed25519PrivateKey.generate()
    release = fixture_release()
    member = release["members"][0]
    member.update(
        pod_id="pod-a",
        source_identity=IDENTITY,
        account_scope=[IDENTITY],
        sizing_policy="FIXED_K_NATIVE_QUANTITY",
        allowed_markets=[MARKET],
        k="0.025",
    )
    release["source_mode"] = "PROSPECTIVE"
    release["definition_sha"] = digest(release["members"])
    release["source_sha"] = digest(
        {
            key: release[key]
            for key in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    signed_release = sign(release, approver, "approver")
    verified = ReleaseVerifier(
        approver.public_key(),
        "approver",
        "fixture-account",
        release["risk_policy_sha"],
        release["rulebook_sha"],
    ).verify(signed_release, now_ms=1000)
    journal = V4ShadowProjection(
        tmp_path / "v4.sqlite",
        release=verified,
        source_key=source.public_key(),
        cutoff_ms=cutoff,
        initial_cursors={IDENTITY: cursor},
    )
    return journal, verified, source


def event(
    key,
    seq,
    known,
    qty=None,
    *,
    kind="OBSERVATION",
    epoch="a" * 32,
    valid=True,
    mark="100",
    identity=IDENTITY,
    scope=None,
):
    if scope is None:
        scope = [identity]
    book = None
    if qty is not None:
        book = {
            "schema": "source_book_v1",
            "source_identity": identity,
            "source_event_ms": known - 1,
            "known_at_ms": known,
            "sequence": seq,
            "scope": scope,
            "scope_complete": True,
            "book_complete": True,
            "nav": None,
            "positions": (
                []
                if qty == 0
                else [
                    {
                        "market": MARKET,
                        "quantity": str(qty),
                        "mark": mark,
                        "mark_known_at_ms": known,
                    }
                ]
            ),
            "source_mode": "PROSPECTIVE",
        }
    proof = {
        "schema": "v4_official_event_proof_v1",
        "source_identity": identity,
        "account_sha": sha256(identity.encode()).hexdigest(),
        "stream_id": "account:pod-a",
        "stream_cursor": seq,
        "stream_chain_sha": digest(["chain", seq]),
        "observer_event_sha": digest(["observer", seq]),
        "stream_epoch": epoch,
        "kind": kind,
        "classification": ("OBSERVED_STATE_CHANGE" if kind == "OBSERVATION" else kind),
        "first_known_ms": known,
        "source_event_ms": known - 1 if book is not None else None,
        "raw_sha": digest(["raw", seq]) if book is not None else None,
        "source_receipt_sha": digest(["source", seq]) if book is not None else None,
        "source_book_sha": digest(book) if book is not None else None,
        "continuity": valid,
        "full_selected_scope": book is not None,
        "trading_authorized": False,
    }
    return {
        "proof": sign(proof, key, "source-observer-v4"),
        "book": sign(book, key, "source-observer") if book is not None else None,
    }


def test_independent_sqlite_cursor_writers_are_fenced_before_predecessor_read(tmp_path):
    """The second process cannot read a stale cursor while writer A holds its lease."""
    import sqlite3

    first, release, signer = setup(tmp_path)
    second = V4ShadowProjection(
        tmp_path / "v4.sqlite",
        release=release,
        source_key=signer.public_key(),
        cutoff_ms=100,
    )
    baseline = event(signer, 1, 101, 0, kind="BASELINE")
    first.con.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            second.ingest(baseline, now_ms=101)
    finally:
        first.con.execute("ROLLBACK")
    assert first.ingest(baseline, now_ms=101) == []
    assert second.ingest(baseline, now_ms=101) == []
    assert (
        second.con.execute("SELECT cursor FROM streams WHERE identity=?", (IDENTITY,)).fetchone()[0]
        == 1
    )
    first.close()
    second.close()


def test_baseline_not_trade_nonresetting_t300_and_duplicate(tmp_path):
    planner, _, key = setup(tmp_path)
    assert planner.ingest(event(key, 1, 101, 0, kind="BASELINE"), now_ms=101) == []
    assert planner.ingest(event(key, 2, 200, 20), now_ms=200) == []
    planner.ingest(event(key, 3, 300, 25), now_ms=300)
    assert planner.due(300199) == []
    action = planner.due(300200)
    assert len(action) == 1
    assert action[0]["kind"] == "ENTRY_ELIGIBLE"
    assert action[0]["first_known_ms"] == 200
    assert action[0]["due_ms"] == 300200
    assert action[0]["eligible_source_qty"] == "25"
    assert action[0]["trading_authorized"] is False
    assert planner.due(300201) == []
    assert planner.ingest(event(key, 3, 300, 25), now_ms=300202) == []
    assert len(planner.actions()) == 1
    planner.close()


def test_close_before_due_cancels_entry(tmp_path):
    planner, _, key = setup(tmp_path)
    planner.ingest(event(key, 1, 101, 0, kind="BASELINE"), now_ms=101)
    planner.ingest(event(key, 2, 200, 20), now_ms=200)
    planner.ingest(event(key, 3, 250, 0), now_ms=250)
    assert planner.due(300200) == []
    assert planner.actions() == []
    planner.close()


def test_source_reduction_now_and_reversal_is_delayed(tmp_path):
    planner, _, key = setup(tmp_path)
    planner.ingest(event(key, 1, 101, 0, kind="BASELINE"), now_ms=101)
    planner.ingest(event(key, 2, 200, 20), now_ms=200)
    planner.due(300200)
    reduced = planner.ingest(event(key, 3, 300300, 10), now_ms=300300)
    assert len(reduced) == 1
    assert reduced[0]["kind"] == "REDUCE_NOW"
    assert reduced[0]["eligible_source_qty"] == "10"
    reversed_events = planner.ingest(event(key, 4, 300400, -5), now_ms=300400)
    assert reversed_events[0]["eligible_source_qty"] == "0"
    assert planner.due(600399) == []
    assert planner.due(600400)[0]["eligible_source_qty"] == "-5"
    planner.close()


def test_gap_rebaseline_and_restart_preserve_pending_fence(tmp_path):
    planner, release, key = setup(tmp_path)
    planner.ingest(event(key, 1, 101, 0, kind="BASELINE"), now_ms=101)
    planner.ingest(event(key, 2, 200, 20), now_ms=200)
    planner.close()
    resumed = V4ShadowProjection(
        tmp_path / "v4.sqlite",
        release=release,
        source_key=key.public_key(),
        cutoff_ms=100,
    )
    resumed.ingest(event(key, 3, 230, kind="GAP", valid=False), now_ms=230)
    resumed.ingest(event(key, 4, 250, 20, kind="BASELINE", epoch="b" * 32), now_ms=250)
    assert resumed.due(300200) == []
    resumed.ingest(event(key, 5, 300300, 25, epoch="b" * 32), now_ms=300300)
    assert resumed.due(600299) == []
    assert resumed.due(600300)[0]["first_known_ms"] == 300300
    resumed.close()


def test_unsigned_or_changed_scope_cannot_create_risk(tmp_path):
    planner, _, key = setup(tmp_path)
    tampered = event(key, 1, 101, 20)
    tampered["proof"]["payload"]["kind"] = "BASELINE"
    with pytest.raises(ValueError):
        planner.ingest(tampered, now_ms=101)
    bad_scope = event(key, 1, 101, 20, scope=["0x" + "b" * 40])
    with pytest.raises(ValueError, match="SCOPE"):
        planner.ingest(bad_scope, now_ms=101)
    with pytest.raises(ValueError, match="CURSOR"):
        planner.ingest(event(key, 3, 301, 20), now_ms=301)
    planner.close()


def test_first_prospective_transition_after_cursor_bootstrap_is_eligible(tmp_path):
    planner, _, key = setup(tmp_path, cutoff=100, cursor=100)
    assert planner.ingest(event(key, 101, 200, 20), now_ms=200) == []
    assert planner.due(300199) == []
    assert planner.due(300200)[0]["eligible_source_qty"] == "20"
    planner.close()


def test_initial_unchanged_is_only_a_baseline_then_new_transition_qualifies(tmp_path):
    planner, _, key = setup(tmp_path)
    assert planner.ingest(event(key, 1, 101, 20, kind="UNCHANGED"), now_ms=101) == []
    assert planner.due(300101) == []
    planner.ingest(event(key, 2, 200, 25), now_ms=200)
    assert planner.due(300200)[0]["eligible_source_qty"] == "25"
    planner.close()


def test_missing_native_mark_is_consumed_as_fenced_event_not_a_replay(tmp_path):
    planner, _, key = setup(tmp_path)
    planner.ingest(event(key, 1, 101, 0, kind="BASELINE"), now_ms=101)
    # Valid official position delta, but no source mark: no price is invented.
    assert planner.ingest(event(key, 2, 200), now_ms=200) == []
    assert planner.due(300200) == []
    # The first complete event after mark loss becomes a diagnostic rebaseline.
    assert planner.ingest(event(key, 3, 250, 10), now_ms=250) == []
    assert planner.due(300250) == []
    planner.ingest(event(key, 4, 300300, 20), now_ms=300300)
    assert planner.due(600299) == []
    assert planner.due(600300)[0]["first_known_ms"] == 300300
    planner.close()


def test_same_cursor_conflicting_signed_chain_is_rejected(tmp_path):
    planner, _, key = setup(tmp_path)
    original = event(key, 1, 101, 0, kind="BASELINE")
    planner.ingest(original, now_ms=101)
    conflicting = event(key, 1, 101, 0, kind="BASELINE")
    from prop_production.evidence import sign

    proof = dict(conflicting["proof"]["payload"])
    proof["stream_chain_sha"] = "f" * 64
    conflicting["proof"] = sign(proof, key, "source-observer-v4")
    with pytest.raises(ValueError, match="EQUIVOCATION"):
        planner.ingest(conflicting, now_ms=101)
    planner.close()


def test_private_shadow_journal_requires_private_parent(tmp_path):
    from prop_production.v4_timing import V4ShadowProjection

    planner, release, key = setup(tmp_path)
    planner.close()
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="PROJECTION_PATH"):
        V4ShadowProjection(
            parent / "v4.sqlite",
            release=release,
            source_key=key.public_key(),
            cutoff_ms=100,
        )


def test_prospective_old_observation_cannot_be_replayed_as_fresh(tmp_path):
    planner, _, key = setup(tmp_path, cutoff=1000, cursor=100)
    with pytest.raises(ValueError, match="CLOCK"):
        planner.ingest(event(key, 101, 999, 20), now_ms=1100)
    planner.close()
