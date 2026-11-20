from dataclasses import replace
from decimal import Decimal

import pytest
from checkpoint_scenarios import quotes, scenario
from test_copy_mandates import START

NOW = START + 300_102


def setup_system(path, opposed=False, second_qty="-15"):
    copies = ((0, "0.025"),)
    if opposed:
        copies += ((1, "0.025" if second_qty == "-15" else "0.015"),)
    scope = scenario(path, copies=copies)
    fixture = scope.__enter__()
    assert fixture.now == NOW
    fixture.guardian.reconcile(NOW)

    class ScenarioClose:
        def close(self):
            scope.__exit__(None, None, None)

        def __getattr__(self, key):
            return getattr(fixture.floor, key)

    return fixture.guardian, fixture.broker, fixture.journal, fixture, ScenarioClose()


def review(guardian, fixture, now=NOW, requests=None):
    return guardian.review_checkpoints(
        fixture.receiver,
        requests if requests is not None else fixture.requests,
        quotes=quotes(now),
        now_ms=now,
    )


def reserve(guardian, fixture, decision_id, now=NOW):
    return guardian.reserve_checkpoint(decision_id, receiver=fixture.receiver, now_ms=now)


def test_checkpoint_review_rederives_finance_and_rejects_unproved_outcome(tmp_path):
    guardian, broker, journal, proof, floor = setup_system(tmp_path)
    try:
        first = review(guardian, proof)
        assert first["status"] == "REVIEWABLE"
        assert review(guardian, proof)["execution_plan"] == first["execution_plan"]
        with pytest.raises(ValueError, match="CHECKPOINT_OWNER_OR_OUTCOME_UNKNOWN"):
            review(guardian, proof, requests={proof.mandates[0].owner: "a" * 64})
        assert not journal.snapshot()["intents"]
        assert broker.audit() == []
    finally:
        floor.close()


def test_durable_intent_precedes_send_and_unknown_is_not_retried(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = review(guardian, proposal, NOW)
    ids = reserve(guardian, proposal, decision["decision_id"], NOW)
    assert ids and journal.snapshot()["reservations"]
    broker.fault_mode = "ACCEPT_THEN_TIMEOUT"
    guardian.dispatch(ids[0], NOW)
    state = journal.snapshot()
    assert state["intents"][ids[0]]["status"] == "UNKNOWN"
    assert state["reservations"]
    assert "UNKNOWN_OUTCOME" in state["halts"]
    with pytest.raises(ValueError):
        guardian.dispatch(ids[0], NOW + 1)
    assert len(broker.audit()) == 1
    guardian.reconcile(NOW + 2)
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
    decision = review(guardian, proposal, NOW)
    ids = reserve(guardian, proposal, decision["decision_id"], NOW)
    broker.connected = False
    assert guardian.reconcile(NOW + 1)["certainty"] == "UNKNOWN"
    with pytest.raises(ValueError):
        guardian.dispatch(ids[0], NOW + 1)
    broker.connected = True
    broker.inject_position("BTC", "1", "10000")
    result = guardian.reconcile(NOW + 2)
    assert result["state"] == "UNRESOLVED"
    assert "ORPHAN_POSITION" in journal.snapshot()["halts"]
    assert result["flat_confirmed"] is False
    journal.close()
    broker.close()
    floor.close()


def test_stale_account_predecessor_and_expired_opportunity_cannot_reserve(tmp_path):
    guardian, broker, journal, proof, floor = setup_system(tmp_path)
    try:
        first = review(guardian, proof)
        guardian.reconcile(NOW + 1)
        with pytest.raises(ValueError, match="CHECKPOINT_RESERVATION_NOT_PERMITTED"):
            reserve(guardian, proof, first["decision_id"], NOW + 1)
        late = review(guardian, proof, first["deadline_ms"])
        assert late["status"] == "MISSED"
        with pytest.raises(ValueError, match="CHECKPOINT_RESERVATION_NOT_PERMITTED"):
            reserve(guardian, proof, late["decision_id"], first["deadline_ms"])
        assert not journal.snapshot()["intents"]
    finally:
        floor.close()


def test_false_filled_terminal_latches_halt_and_keeps_reservation(tmp_path):
    """Outside provider evidence contradicts FILLED despite a coherent cutoff."""
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = review(guardian, proposal, NOW)
    intent_id = reserve(guardian, proposal, decision["decision_id"], NOW)[0]
    broker.fill_on_submit = False
    guardian.dispatch(intent_id, NOW)
    broker.fill(intent_id, "0.6")
    snapshot = broker.observe(NOW + 1)
    assert snapshot.orders[0].status == "PARTIAL"
    assert snapshot.orders[0].filled != snapshot.orders[0].quantity
    false_terminal = replace(snapshot.orders[0], status="FILLED")
    guardian.apply_snapshot(replace(snapshot, orders=(false_terminal,)), NOW + 1)
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
    decision = review(guardian, proposal, NOW)
    intent_id = reserve(guardian, proposal, decision["decision_id"], NOW)[0]
    broker.fill_on_submit = False
    guardian.dispatch(intent_id, NOW)
    snapshot = broker.observe(NOW + 1)
    unknown = replace(snapshot.orders[0], status="PROVIDER_NEW_TERMINAL")
    guardian.apply_snapshot(replace(snapshot, orders=(unknown,)), NOW + 1)
    state = journal.snapshot()
    assert "BROKER_STATUS_UNKNOWN" in state["halts"]
    assert state["reservations"]
    assert state["intents"][intent_id]["status"] != "PROVIDER_NEW_TERMINAL"
    journal.close()
    broker.close()
    floor.close()


def test_duplicate_fills_are_deduped_and_conflicts_halt(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = review(guardian, proposal, NOW)
    ids = reserve(guardian, proposal, decision["decision_id"], NOW)
    guardian.dispatch(ids[0], NOW)
    guardian.reconcile(NOW + 1)
    before = journal.snapshot()["positions"]
    snapshot = broker.observe(NOW + 2)
    guardian.apply_snapshot(replace(snapshot, fills=snapshot.fills + snapshot.fills), NOW + 2)
    assert journal.snapshot()["positions"] == before
    bad_fill = replace(snapshot.fills[0], quantity=Decimal("999"))
    guardian.apply_snapshot(replace(snapshot, fills=(bad_fill,)), NOW + 2)
    assert "FILL_ID_CONFLICT" in journal.snapshot()["halts"]
    journal.close()
    broker.close()
    floor.close()
