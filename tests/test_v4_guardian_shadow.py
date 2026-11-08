"""Guardian must independently apply account risk and never reserve V4 shadow decisions."""

import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_v4_timing import IDENTITY, event

from prop_production.evidence import digest, sign
from prop_production.finance import Rulebook
from prop_production.guardian import Guardian
from prop_production.journal import Journal
from prop_production.judge.broker import FakeBroker
from prop_production.release import ReleaseFloor, ReleaseVerifier, fixture_release
from prop_production.risk import fixture_markets, fixture_policy
from prop_production.v4_timing import V4ShadowProjection

NATIVE = "hyperliquid/mainnet:perp:default/BTC"


def guardian_and_projection(tmp_path):
    approver = Ed25519PrivateKey.generate()
    source = Ed25519PrivateKey.generate()
    rules = Rulebook.synthetic()
    policy, markets = fixture_policy(), fixture_markets()
    policy["source_mode"] = "PROSPECTIVE"
    markets[NATIVE] = copy.deepcopy(markets["BTC"])
    markets[NATIVE]["source_mode"] = "PROSPECTIVE"
    for spec in markets.values():
        spec["source_mode"] = "PROSPECTIVE"
    release = fixture_release()
    member = release["members"][0]
    member.update(
        pod_id="pod-a",
        source_identity=IDENTITY,
        account_scope=[IDENTITY],
        allowed_markets=[NATIVE],
        sizing_policy="FIXED_K_NATIVE_QUANTITY",
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
    release["risk_policy_sha"] = digest({"policy": policy, "markets": markets})
    release["rulebook_sha"] = rules.sha
    verified = ReleaseVerifier(
        approver.public_key(),
        "approver",
        "fixture-account",
        release["risk_policy_sha"],
        rules.sha,
    ).verify(sign(release, approver, "approver"), now_ms=1000)
    floor = ReleaseFloor(tmp_path / "floor.sqlite", initialize=True)
    floor.accept(verified)
    journal = Journal(tmp_path / "account.sqlite", "fixture-account", initialize=True)
    broker = FakeBroker(tmp_path / "broker.sqlite")
    guardian = Guardian(
        journal,
        verified,
        floor,
        source.public_key(),
        rules,
        policy,
        markets,
        broker,
    )
    projector = V4ShadowProjection(
        tmp_path / "v4.sqlite",
        release=verified,
        source_key=source.public_key(),
        cutoff_ms=100,
    )
    return guardian, projector, source, broker, journal, floor


def test_shadow_guardian_denial_is_journaled_and_cannot_be_reserved(tmp_path):
    guardian, projection, source, broker, journal, floor = guardian_and_projection(tmp_path)
    projection.ingest(event(source, 1, 101, 0, kind="BASELINE"), now_ms=101)
    projection.ingest(event(source, 2, 200, 20), now_ms=200)
    assert projection.due(300199) == []
    decision_action = projection.due(300200)[0]
    quote = {NATIVE: {"bid": "10000", "ask": "10030", "known_at_ms": 300200}}
    result = guardian.preview_v4(projection, decision_action["action_id"], quote, 300200)
    assert result.data["schema"] == "guardian_v4_shadow_decision_v1"
    assert result.data["status"] == "BLOCKED"
    assert "RECONCILIATION_UNCERTAIN" in result.data["reasons"]
    assert result.data["trading_authorized"] is False
    assert result.data["broker_mutations"] == 0
    with pytest.raises(ValueError, match="RESERVATION_NOT_PERMITTED"):
        guardian.reserve(result.data["decision_id"], 300200)
    assert not journal.snapshot()["intents"]
    assert not journal.snapshot()["reservations"]
    # Retrying the same source action is idempotent and writes no second decision.
    seq = journal.sequence
    assert (
        guardian.preview_v4(projection, decision_action["action_id"], quote, 300200).payload
        == result.payload
    )
    assert journal.sequence == seq
    projection.close()
    journal.close()
    broker.close()
    floor.close()


def test_shadow_guardian_rejects_different_signer_and_expired_timing(tmp_path):
    guardian, projection, source, broker, journal, floor = guardian_and_projection(tmp_path)
    projection.ingest(event(source, 1, 101, 0, kind="BASELINE"), now_ms=101)
    projection.ingest(event(source, 2, 200, 20), now_ms=200)
    action = projection.due(300200)[0]
    bad_projection = copy.copy(projection)
    bad_projection.source_key = Ed25519PrivateKey.generate().public_key()
    with pytest.raises(ValueError, match="PROJECTION_AUTHORITY"):
        guardian.preview_v4(bad_projection, action["action_id"], {}, 300200)
    with pytest.raises(ValueError, match="NOT_ELIGIBLE"):
        guardian.preview_v4(projection, action["action_id"], {}, 300199)
    projection.close()
    journal.close()
    broker.close()
    floor.close()
