from dataclasses import replace
from decimal import Decimal

import pytest
from test_planner import book, quote


def setup_system(tmp_path, opposed=False, second_qty="-15"):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from prop_production.evidence import digest, sign
    from prop_production.finance import Rulebook
    from prop_production.guardian import Guardian
    from prop_production.journal import Journal
    from prop_production.judge.broker import FakeBroker
    from prop_production.planner import Planner
    from prop_production.release import ReleaseFloor, ReleaseVerifier, fixture_release
    from prop_production.risk import fixture_markets, fixture_policy
    key = Ed25519PrivateKey.generate()
    rules = Rulebook.synthetic()
    policy, markets = fixture_policy(), fixture_markets()
    body = fixture_release()
    body["risk_policy_sha"] = digest({"policy": policy, "markets": markets})
    body["rulebook_sha"] = rules.sha
    if opposed:
        body["members"].append(dict(body["members"][0], pod_id="pod-b",
            source_identity="fixture-leader-b", account_scope=["fixture-leader-b"]))
        body["definition_sha"] = digest(body["members"])
        body["source_sha"] = digest({k: body[k] for k in
            ("research_commit", "decision_sha", "inventory_sha", "definition_sha", "selection_known_at_ms")})
    release = ReleaseVerifier(key.public_key(), "approver", "fixture-account",
                              body["risk_policy_sha"], rules.sha).verify(
                                  sign(body, key, "approver"), now_ms=1000)
    floor = ReleaseFloor(tmp_path / "release.sqlite", initialize=True)
    floor.accept(release)
    journal = Journal(tmp_path / "account.sqlite", "fixture-account", initialize=True)
    broker = FakeBroker(tmp_path / "broker.sqlite")
    guardian = Guardian(journal, release, floor, key.public_key(), rules, policy, markets, broker)
    planner = Planner(tmp_path / "planner.sqlite", release, key.public_key())
    planner.ingest(sign(book(), key, "source-observer"), local_known_ms=1000)
    if opposed:
        second = dict(book(qty=second_qty), source_identity="fixture-leader-b", scope=["fixture-leader-b"])
        planner.ingest(sign(second, key, "source-observer"), local_known_ms=1000)
    guardian.reconcile(301000)
    proposal = planner.propose(301000, journal.snapshot()["runtime"]["broker_epoch"], quote())
    planner.close()
    return guardian, broker, journal, proposal, floor


def test_preview_idempotent_and_compromised_planner_has_no_equity_authority(tmp_path):
    from prop_production.contracts import TargetBatchProposal
    from prop_production.evidence import digest
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    first = guardian.decide(proposal, quote(), 301000)
    assert first.data["status"] == "PREVIEW"
    assert guardian.decide(proposal, quote(), 301001).payload == first.payload
    assert not journal.snapshot()["intents"]
    bad = proposal.data
    bad["equity"] = "100000000"
    with pytest.raises(ValueError, match="MALFORMED"):
        guardian.decide(TargetBatchProposal(__import__("json").dumps(bad).encode()), quote(), 301000)
    bad = proposal.data
    bad["pods"][0]["requested_notionals"]["BTC"] = "1"
    bad["proposal_id"] = digest({k: v for k, v in bad.items() if k != "proposal_id"})
    receipt = guardian.decide(TargetBatchProposal.from_dict(bad), quote(), 301000)
    assert receipt.data["status"] == "REJECTED"
    assert "SOURCE_REQUEST_MISMATCH" in receipt.data["reasons"]
    assert broker.audit() == []
    journal.close()
    broker.close()
    floor.close()


def test_durable_intent_precedes_send_and_unknown_is_not_retried(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = guardian.decide(proposal, quote(), 301000)
    ids = guardian.reserve(decision.data["decision_id"], 301000)
    assert ids and journal.snapshot()["reservations"]
    broker.fault_mode = "ACCEPT_THEN_TIMEOUT"
    guardian.dispatch(ids[0], 301000)
    state = journal.snapshot()
    assert state["intents"][ids[0]]["status"] == "UNKNOWN"
    assert state["reservations"]
    assert "UNKNOWN_OUTCOME" in state["halts"]
    with pytest.raises(ValueError):
        guardian.dispatch(ids[0], 301001)
    assert len(broker.audit()) == 1
    guardian.reconcile(301002)
    state = journal.snapshot()
    assert state["intents"][ids[0]]["status"] == "FILLED"
    assert "UNKNOWN_OUTCOME" in state["halts"]
    assert not state["reservations"]
    assert Decimal(state["positions"]["BTC"]) > 0
    journal.close()
    broker.close()
    floor.close()


def test_stale_cache_or_gap_invalidates_permit_and_orphan_is_not_flat(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = guardian.decide(proposal, quote(), 301000)
    ids = guardian.reserve(decision.data["decision_id"], 301000)
    broker.connected = False
    assert guardian.reconcile(301001)["certainty"] == "UNKNOWN"
    with pytest.raises(ValueError):
        guardian.dispatch(ids[0], 301001)
    broker.connected = True
    broker.inject_position("BTC", "1", "10000")
    result = guardian.reconcile(301002)
    assert result["state"] == "UNRESOLVED"
    assert "ORPHAN_POSITION" in journal.snapshot()["halts"]
    assert result["flat_confirmed"] is False
    journal.close()
    broker.close()
    floor.close()


def test_replay_old_epoch_and_expired_release_cannot_reserve(tmp_path):
    from prop_production.contracts import TargetBatchProposal
    from prop_production.evidence import digest
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    data = proposal.data
    data["broker_epoch"] -= 1
    data["proposal_id"] = digest({k: v for k, v in data.items() if k != "proposal_id"})
    receipt = guardian.decide(TargetBatchProposal.from_dict(data), quote(), 301000)
    assert "BROKER_EPOCH" in receipt.data["reasons"]
    late = guardian.decide(proposal, quote(), 1000001)
    assert "RELEASE_EXPIRED" in late.data["reasons"]
    with pytest.raises(ValueError):
        guardian.reserve(late.data["decision_id"], 1000001)
    assert not journal.snapshot()["intents"]
    journal.close()
    broker.close()
    floor.close()


def test_false_filled_terminal_latches_halt_and_keeps_reservation(tmp_path):
    """Outside provider evidence contradicts FILLED despite a coherent cutoff."""
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = guardian.decide(proposal, quote(), 301000)
    intent_id = guardian.reserve(decision.data["decision_id"], 301000)[0]
    broker.fill_on_submit = False
    guardian.dispatch(intent_id, 301000)
    broker.fill(intent_id, "0.6")
    snapshot = broker.observe(301001)
    assert snapshot.orders[0].status == "PARTIAL"
    assert snapshot.orders[0].filled != snapshot.orders[0].quantity
    false_terminal = replace(snapshot.orders[0], status="FILLED")
    guardian.apply_snapshot(replace(snapshot, orders=(false_terminal,)), 301001)
    state = journal.snapshot()
    assert "BROKER_FALSE_FILLED_TERMINAL" in state["halts"]
    assert state["runtime"]["state"] == "UNRESOLVED"
    assert state["runtime"]["permit"] is None
    assert state["reservations"]
    assert state["intents"][intent_id]["status"] != "FILLED"
    assert Decimal(state["intents"][intent_id]["filled"]) == snapshot.orders[0].filled
    assert broker.audit() and len([row for row in broker.audit() if row["kind"] == "SUBMIT"]) == 1
    journal.close()
    broker.close()
    floor.close()


def test_unknown_broker_status_halts_without_releasing_reservation(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = guardian.decide(proposal, quote(), 301000)
    intent_id = guardian.reserve(decision.data["decision_id"], 301000)[0]
    broker.fill_on_submit = False
    guardian.dispatch(intent_id, 301000)
    snapshot = broker.observe(301001)
    unknown = replace(snapshot.orders[0], status="PROVIDER_NEW_TERMINAL")
    guardian.apply_snapshot(replace(snapshot, orders=(unknown,)), 301001)
    state = journal.snapshot()
    assert "BROKER_STATUS_UNKNOWN" in state["halts"]
    assert state["reservations"]
    assert state["intents"][intent_id]["status"] != "PROVIDER_NEW_TERMINAL"
    journal.close()
    broker.close()
    floor.close()


def test_duplicate_fills_are_deduped_and_conflicts_halt(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = guardian.decide(proposal, quote(), 301000)
    ids = guardian.reserve(decision.data["decision_id"], 301000)
    guardian.dispatch(ids[0], 301000)
    guardian.reconcile(301001)
    before = journal.snapshot()["positions"]
    snapshot = broker.observe(301002)
    guardian.apply_snapshot(replace(snapshot, fills=snapshot.fills + snapshot.fills), 301002)
    assert journal.snapshot()["positions"] == before
    bad_fill = replace(snapshot.fills[0], quantity=Decimal("999"))
    guardian.apply_snapshot(replace(snapshot, fills=(bad_fill,)), 301002)
    assert "FILL_ID_CONFLICT" in journal.snapshot()["halts"]
    journal.close()
    broker.close()
    floor.close()
