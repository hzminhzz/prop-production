"""The Guardian independently recomputes fixed-k NAV-free intents and risk caps."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_k_sizing import fixed_k_release
from test_planner import book, quote

from prop_production.evidence import digest, sign
from prop_production.finance import Rulebook
from prop_production.guardian import Guardian
from prop_production.journal import Journal
from prop_production.judge.broker import FakeBroker
from prop_production.planner import Planner
from prop_production.release import ReleaseFloor, ReleaseVerifier
from prop_production.risk import fixture_markets, fixture_policy


def test_guardian_recomputes_k_intent_without_source_nav_and_keeps_risk_controls(tmp_path):
    key = Ed25519PrivateKey.generate()
    rules = Rulebook.synthetic()
    policy, markets = fixture_policy(), fixture_markets()
    doc, _ = fixed_k_release(key, k="0.04")
    doc["risk_policy_sha"] = digest({"policy": policy, "markets": markets})
    doc["rulebook_sha"] = rules.sha
    release = ReleaseVerifier(
        key.public_key(), "approver", "fixture-account", doc["risk_policy_sha"], doc["rulebook_sha"]
    ).verify(sign(doc, key, "approver"), now_ms=1000)

    floor = ReleaseFloor(tmp_path / "release.sqlite", initialize=True)
    floor.accept(release)
    journal = Journal(tmp_path / "account.sqlite", "fixture-account", initialize=True)
    broker = FakeBroker(tmp_path / "broker.sqlite")
    try:
        guardian = Guardian(
            journal, release, floor, key.public_key(), rules, policy, markets, broker
        )
        planner = Planner(tmp_path / "planner.sqlite", release, key.public_key())
        try:
            source = book(qty="1")
            source["nav"] = None
            planner.ingest(sign(source, key, "source-observer"), local_known_ms=1000)
            guardian.reconcile(301000)
            proposal = planner.propose(
                301000, journal.snapshot()["runtime"]["broker_epoch"], quote()
            )
            assert proposal.data["pods"][0]["source_nav"] is None
            assert proposal.data["pods"][0]["requested_notionals"]["BTC"] == "400.00"
            decision = guardian.decide(proposal, quote(), 301000)
            assert decision.data["status"] == "PREVIEW"
            assert decision.data["risk"]["status"] == "ACCEPTED"
            assert not decision.data["trading_authorized"]
            assert journal.snapshot()["runtime"]["permit"] is None
            assert broker.audit() == []
        finally:
            planner.close()
    finally:
        journal.close()
        broker.close()
        floor.close()
