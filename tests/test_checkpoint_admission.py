"""S2 one-shot financial authority through the existing Guardian/Gateway."""

import json

import pytest
import test_checkpoint_guardian as checkpoint_fixture
from test_checkpoint_guardian import prime
from test_checkpoint_receiver import fixture_packet
from test_planner import quote

system = checkpoint_fixture.system


def request(system):
    mandate, now = prime(system)
    outcome = json.loads(fixture_packet())["payload"]["transitions"][-1]["effect"]
    return mandate, now, outcome


def review(system, mandate, outcome, now, quotes=None):
    return system.guardian.review_checkpoints(
        system.receiver,
        {mandate.owner: outcome["outcome_id"]},
        quotes=quote(now) if quotes is None else quotes,
        now_ms=now,
    )


def test_guardian_commits_one_opportunity_and_reconciles_independent_fill(system):
    m, now, outcome = request(system)
    system.guardian.reconcile(now)
    decision = review(system, m, outcome, now)
    assert decision["status"] == "REVIEWABLE"
    assert decision["risk"]["pod_targets"][m.owner]["BTC"] == "0.07477"
    assert system.broker.audit() == []
    ids = system.guardian.reserve_checkpoint(
        decision["decision_id"], receiver=system.receiver, now_ms=now + 1
    )
    assert len(ids) == 1
    assert (
        system.guardian.reserve_checkpoint(
            decision["decision_id"], receiver=system.receiver, now_ms=now + 1
        )
        == ids
    )
    system.guardian.dispatch(ids[0], now + 2)
    system.guardian.reconcile(now + 3)
    assert system.journal.snapshot()["positions"]["BTC"] == "0.07477"
    repeated = review(system, m, outcome, now + 4)
    assert repeated["status"] == "COMMITTED_ONCE"
    assert len(system.broker.audit()) == 1
    system.journal.verify()


def test_predispatch_missing_account_and_quote_can_retry_only_within_original_deadline(system):
    m, now, outcome = request(system)
    first = review(system, m, outcome, now, {})
    assert first["status"] == "PRE_DISPATCH_BLOCKED"
    deadline = first["deadline_ms"]
    system.guardian.reconcile(now + 1)
    ready = review(system, m, outcome, now + 2)
    assert ready["status"] == "REVIEWABLE"
    assert ready["deadline_ms"] == deadline
    assert not system.journal.snapshot()["intents"]
    expired = review(system, m, outcome, deadline)
    assert expired["status"] == "MISSED"
    with pytest.raises(ValueError):
        system.guardian.reserve_checkpoint(
            ready["decision_id"], receiver=system.receiver, now_ms=deadline
        )
    assert system.broker.audit() == []


def test_reservation_boundary_durably_expires_uncommitted_opportunity(system):
    m, now, outcome = request(system)
    system.guardian.reconcile(now)
    decision = review(system, m, outcome, now)
    with pytest.raises(ValueError, match="CHECKPOINT_RESERVATION_NOT_PERMITTED"):
        system.guardian.reserve_checkpoint(
            decision["decision_id"], receiver=system.receiver, now_ms=decision["deadline_ms"]
        )
    state = system.journal.snapshot()
    assert state["checkpoint_decisions"][decision["decision_id"]]["result"]["status"] == "MISSED"
    assert next(iter(state["checkpoint_opportunities"].values()))["state"]["phase"] == "MISSED"
    assert not state["intents"]
    assert system.broker.audit() == []
    system.journal.verify()


def test_unknown_submission_never_creates_second_intent_or_retries(system):
    m, now, outcome = request(system)
    system.guardian.reconcile(now)
    decision = review(system, m, outcome, now)
    ids = system.guardian.reserve_checkpoint(
        decision["decision_id"], receiver=system.receiver, now_ms=now + 1
    )
    system.broker.fault_mode = "ACCEPT_THEN_TIMEOUT"
    system.guardian.dispatch(ids[0], now + 2)
    state = system.journal.snapshot()
    assert state["intents"][ids[0]]["status"] == "UNKNOWN"
    assert state["reservations"]
    assert "UNKNOWN_OUTCOME" in state["halts"]
    assert review(system, m, outcome, now + 3)["status"] == "COMMITTED_ONCE"
    with pytest.raises(ValueError):
        system.guardian.dispatch(ids[0], now + 4)
    assert len(system.broker.audit()) == 1


def test_halt_and_unregistered_owner_are_not_temporary_readiness(system):
    m, now, outcome = request(system)
    system.guardian.reconcile(now)
    system.journal.halt("risk stop", now)
    assert review(system, m, outcome, now)["status"] == "MISSED"
    with pytest.raises(ValueError):
        system.guardian.review_checkpoints(
            system.receiver, {"f" * 64: outcome["outcome_id"]}, quotes=quote(now), now_ms=now
        )
    assert system.broker.audit() == []


def test_readonly_gateway_cannot_reserve_even_synthetic_source_authority(system):
    from prop_production.gateway import ReadOnlyGateway

    m, now, outcome = request(system)
    system.guardian.reconcile(now)
    system.guardian.gateway = ReadOnlyGateway(
        lambda: system.broker.observe(now), "fixture-account", "fake://broker"
    )
    decision = review(system, m, outcome, now)
    assert decision["status"] == "PRE_DISPATCH_BLOCKED"
    assert "EXECUTION_CAPABILITY_UNQUALIFIED" in decision["reasons"]
    with pytest.raises(PermissionError, match="REMOTE_MUTATION"):
        system.guardian.reserve_checkpoint(
            decision["decision_id"], receiver=system.receiver, now_ms=now
        )
    assert not system.journal.snapshot()["intents"]
    assert system.broker.audit() == []


def test_financial_oracle_rejects_intent_larger_than_verified_target(system, monkeypatch):
    m, now, outcome = request(system)
    system.guardian.reconcile(now)
    decision = review(system, m, outcome, now)
    original = system.journal.commit

    def forged(kind, at_ms, state, **kwargs):
        if kind == "CHECKPOINT_RESERVE":
            intent = next(iter(state["intents"].values()))
            intent["quantity"] = "1"
            intent["pod_changes"] = {m.owner: "1"}
        return original(kind, at_ms, state, **kwargs)

    monkeypatch.setattr(system.journal, "commit", forged)
    with pytest.raises(ValueError, match="CHECKPOINT_INTENT_DERIVATION"):
        system.guardian.reserve_checkpoint(
            decision["decision_id"], receiver=system.receiver, now_ms=now + 1
        )
    assert not system.journal.snapshot()["intents"]
    assert system.broker.audit() == []
