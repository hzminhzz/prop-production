"""Reversal and reduction against independent broker cash, basis and executions."""

from decimal import Decimal as D
from fractions import Fraction

import pytest
from checkpoint_scenarios import quotes, scenario
from test_checkpoint_netting import decide, reserve


def opened(s):
    decision = decide(s)
    ids = reserve(s, decision)
    s.guardian.dispatch(ids[0], s.now + 2)
    s.guardian.reconcile(s.now + 3)
    assert not s.journal.snapshot()["halts"]
    return ids[0]


def next_decision(s, index):
    s.now, s.requests = s.wave(index)
    s.guardian.reconcile(s.now)
    return s.guardian.review_checkpoints(s.receiver, s.requests, quotes=quotes(s.now), now_ms=s.now)


@pytest.mark.parametrize("cross_zero", [True, False])
def test_cross_zero_uses_one_order_or_bounded_reduce_only_without_target_chase(
    tmp_path, cross_zero
):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.wave(2)
        s.broker.cross_zero_supported = cross_zero
        decision = next_decision(s, 3)
        before = s.broker.observe(s.now)
        assert decision["status"] == "REVIEWABLE"
        ids = reserve(s, decision)
        assert len(ids) == 1
        intent = s.journal.snapshot()["intents"][ids[0]]
        assert D(intent["quantity"]) == (D("-0.125") if cross_zero else D("-0.075"))
        assert intent["reduce_only"] is (not cross_zero)
        s.guardian.dispatch(ids[0], s.now + 2)
        s.guardian.reconcile(s.now + 3)
        state = s.journal.snapshot()
        actual = s.broker.observe(s.now + 3)
        assert not state["halts"]
        assert not state["reservations"]
        assert D(state["positions"]["BTC"]) == (D("-0.05") if cross_zero else D("0"))
        closing_fill = actual.fills[-1]
        realized = D("0.075") * (closing_fill.price - before.positions[0].entry_price)
        assert actual.cash - before.cash == realized - closing_fill.fee
        effects = state["checkpoint_allocations"][closing_fill.execution_id]["effects"]
        assert sum(Fraction(e["realized"]) for e in effects.values()) == Fraction(realized)
        assert sum(Fraction(e["cash_delta"]) for e in effects.values()) == Fraction(
            actual.cash - before.cash
        )
        committed = s.guardian.review_checkpoints(
            s.receiver, s.requests, quotes=quotes(s.now + 4), now_ms=s.now + 4
        )
        assert committed["status"] == "COMMITTED_ONCE"
        if not cross_zero:
            assert committed["opening_status"] == "MISSED"
            matching = [
                row
                for row in state["checkpoint_opportunities"].values()
                if row["outcome_id"] in s.requests.values()
            ]
            assert all(row["state"]["phase"] == "MISSED" for row in matching)
        assert len([row for row in s.broker.audit() if row["kind"] == "SUBMIT"]) == 2
        s.journal.verify()


def test_verified_source_reduction_and_exit_reconcile_actual_realized_cash(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        reduced = next_decision(s, 2)
        ids = reserve(s, reduced)
        assert s.journal.snapshot()["intents"][ids[0]]["reduce_only"] is True
        s.guardian.dispatch(ids[0], s.now + 2)
        s.guardian.reconcile(s.now + 3)
        assert not s.journal.snapshot()["halts"]
        assert D(s.journal.snapshot()["positions"]["BTC"]) == D("0.025")
        s.wave(3)
        exited = next_decision(s, 4)
        ids = reserve(s, exited)
        s.guardian.dispatch(ids[0], s.now + 2)
        s.guardian.reconcile(s.now + 3)
        assert not s.broker.observe(s.now + 3).positions
        assert not s.journal.snapshot()["halts"]
        assert s.journal.snapshot()["runtime"]["flat_confirmed"] is True


def test_partial_cross_zero_cancel_never_retries_remaining_target(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.wave(2)
        s.broker.fill_on_submit = False
        decision = next_decision(s, 3)
        ids = reserve(s, decision)
        s.guardian.dispatch(ids[0], s.now + 2)
        s.broker.fill(ids[0], "0.4", "9970")
        s.guardian.reconcile(s.now + 3)
        assert D(s.journal.snapshot()["positions"]["BTC"]) == D("0.025")
        s.broker.fault_mode = "PARTIAL_DURING_CANCEL"
        s.guardian.cancel(ids[0], s.now + 4)
        s.guardian.reconcile(s.now + 5)
        state = s.journal.snapshot()
        assert not state["halts"]
        assert D(state["positions"]["BTC"]) < 0
        assert state["intents"][ids[0]]["status"] == "CANCELLED"
        assert not state["reservations"]
        again = s.guardian.review_checkpoints(
            s.receiver, s.requests, quotes=quotes(s.now + 6), now_ms=s.now + 6
        )
        assert again["status"] == "COMMITTED_ONCE"
        assert len(state["intents"]) == 2
        s.journal.verify()


def test_wrong_independent_entry_basis_fences_close_and_preserves_obligations(tmp_path):
    from dataclasses import replace

    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.now, s.requests = s.wave(2)
        s.guardian.reconcile(s.now)
        reader = s.broker.observe

        def wrong_basis(now):
            observed = reader(now)
            positions = tuple(
                replace(p, entry_price=p.entry_price + 100) for p in observed.positions
            )
            return replace(observed, positions=positions)

        s.broker.observe = wrong_basis
        decision = s.guardian.review_checkpoints(
            s.receiver, s.requests, quotes=quotes(s.now), now_ms=s.now
        )
        assert decision["status"] == "MISSED"
        assert "INDEPENDENT_ENTRY_BASIS_UNQUALIFIED" in decision["reasons"]
        assert len(s.journal.snapshot()["intents"]) == 1
        assert D(s.journal.snapshot()["positions"]["BTC"]) == D("0.075")


def test_capability_change_after_review_requires_fresh_projection_before_commit(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.wave(2)
        decision = next_decision(s, 3)
        s.broker.cross_zero_supported = False
        with pytest.raises(ValueError, match="EXECUTION_PROOF_CHANGED"):
            reserve(s, decision)
        assert len(s.journal.snapshot()["intents"]) == 1
        revised = s.guardian.review_checkpoints(
            s.receiver, s.requests, quotes=quotes(s.now + 1), now_ms=s.now + 1
        )
        assert revised["deadline_ms"] == decision["deadline_ms"]
        assert revised["execution_plan"]["opening_status"] == "MISSED"


def test_unqualified_cross_and_reduce_semantics_cannot_create_an_adjustment(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.wave(2)
        s.broker.cross_zero_supported = False
        s.broker.reduce_only_supported = False
        decision = next_decision(s, 3)
        assert decision["status"] == "MISSED"
        assert "REDUCE_ONLY_CAPABILITY_UNQUALIFIED" in decision["reasons"]
        assert len(s.journal.snapshot()["intents"]) == 1
        assert D(s.broker.observe(s.now).positions[0].quantity) == D("0.075")
