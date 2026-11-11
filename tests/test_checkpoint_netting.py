"""Outside conservation checks use only actual fills and Decimal/Fraction arithmetic."""

import random
from decimal import Decimal as D
from fractions import Fraction

import pytest
from checkpoint_scenarios import quotes, scenario


def decide(s, requests=None):
    s.guardian.reconcile(s.now)
    return s.guardian.review_checkpoints(
        s.receiver, requests or s.requests, quotes=quotes(s.now), now_ms=s.now
    )


def reserve(s, decision):
    return s.guardian.reserve_checkpoint(
        decision["decision_id"], receiver=s.receiver, now_ms=s.now + 1
    )


def test_net_order_partial_fill_and_cancel_conserve_individual_pod_liabilities(tmp_path):
    with scenario(tmp_path) as s:
        s.broker.fill_on_submit = False
        result = decide(s)
        assert result["status"] == "REVIEWABLE"
        ids = reserve(s, result)
        assert len(ids) == 1
        state = s.journal.snapshot()
        assert D(state["intents"][ids[0]]["quantity"]) == D("0.045")
        s.guardian.dispatch(ids[0], s.now + 2)
        s.broker.fill(ids[0], "0.3333333333333333333333333333", "10000")
        s.guardian.reconcile(s.now + 3)
        s.broker.fault_mode = "PARTIAL_DURING_CANCEL"
        s.guardian.cancel(ids[0], s.now + 4)
        s.guardian.reconcile(s.now + 5)
        state = s.journal.snapshot()
        actual = s.broker.observe(s.now + 5)
        assert not state["halts"]
        assert not state["reservations"]
        assert state["intents"][ids[0]]["status"] == "CANCELLED"
        assert sum(
            Fraction(book.get("BTC", "0")) for book in state["pod_positions"].values()
        ) == Fraction(actual.positions[0].quantity)
        assert sum(Fraction(v) for v in state["pod_fees_paid"].values()) == sum(
            Fraction(f.fee) for f in actual.fills
        )
        effects = state["checkpoint_allocations"]
        assert len(effects) == len(actual.fills)
        for fill in actual.fills:
            allocations = effects[fill.execution_id]["effects"]
            assert sum(Fraction(a["quantity"]) for a in allocations.values()) == Fraction(
                fill.quantity
            )
            assert sum(Fraction(a["notional"]) for a in allocations.values()) == Fraction(
                fill.quantity
            ) * Fraction(fill.price)
            assert sum(Fraction(a["fee"]) for a in allocations.values()) == Fraction(fill.fee)
            assert sum(Fraction(a["cash_delta"]) for a in allocations.values()) == -Fraction(
                fill.fee
            )
        assert (
            s.guardian.review_checkpoints(
                s.receiver, s.requests, quotes=quotes(s.now + 6), now_ms=s.now + 6
            )["status"]
            == "COMMITTED_ONCE"
        )
        assert len([a for a in s.broker.audit() if a["kind"] == "SUBMIT"]) == 1
        s.journal.verify()


def test_zero_net_records_liabilities_without_trade_fill_fee_or_cash_change(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"), (1, "0.025"))) as s:
        result = decide(s)
        before = s.journal.snapshot()
        assert D(result["risk"]["targets"]["BTC"]) == 0
        assert reserve(s, result) == []
        state = s.journal.snapshot()
        assert sorted(D(book["BTC"]) for book in state["pod_positions"].values()) == [
            D("-0.075"),
            D("0.075"),
        ]
        assert state["positions"] == before["positions"]
        assert state["finance"] == before["finance"]
        assert not state["fills"]
        assert state["runtime"]["permit"] is None
        assert not state.get("pod_fees_paid")
        assert not state.get("pod_realized")
        assert not s.broker.audit()
        assert state["checkpoint_internal_effects"]
        s.journal.verify()


def test_opposing_pods_cannot_hide_gross_exposure_behind_zero_net(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"), (1, "0.025")), gross_limit="1000") as s:
        result = decide(s)
        assert result["status"] == "REVIEWABLE"
        gross = sum(
            abs(D(v) * D("10000"))
            for book in result["risk"]["pod_targets"].values()
            for v in book.values()
        )
        assert gross <= D("1000")
        assert D(result["risk"]["gross_notional"]) == gross
        assert D(result["risk"]["cost_reserve"]) > 0
        assert D(result["risk"]["worst_loss"]) > 0


def test_request_arrival_order_does_not_change_one_net_adjustment(tmp_path):
    observed = []
    for seed in range(4):
        with scenario(tmp_path / str(seed)) as s:
            entries = list(s.requests.items())
            random.Random(seed).shuffle(entries)
            result = decide(s, dict(entries))
            ids = reserve(s, result)
            state = s.journal.snapshot()
            observed.append(
                (
                    result["risk"],
                    [
                        (state["intents"][key]["quantity"], state["intents"][key]["pod_changes"])
                        for key in ids
                    ],
                )
            )
    assert all(value == observed[0] for value in observed)


def test_separate_batch_cannot_fragment_one_market_checkpoint_into_two_orders(tmp_path):
    with scenario(tmp_path, copies=((0, "0.015"), (0, "0.01"))) as s:
        first, second = s.mandates
        decision = decide(s, {first.owner: s.requests[first.owner]})
        ids = reserve(s, decision)
        s.guardian.dispatch(ids[0], s.now + 2)
        s.guardian.reconcile(s.now + 3)
        later = s.guardian.review_checkpoints(
            s.receiver,
            {second.owner: s.requests[second.owner]},
            quotes=quotes(s.now + 4),
            now_ms=s.now + 4,
        )
        assert later["status"] == "MISSED"
        assert "MARKET_CHECKPOINT_ALREADY_COMMITTED" in later["reasons"]
        assert len(s.journal.snapshot()["intents"]) == 1


def test_paused_mandate_does_not_fence_unaffected_active_consumer(tmp_path):
    from prop_production.evidence import digest, sign

    with scenario(tmp_path) as s:
        paused = s.mandates[1]
        state = s.journal.snapshot()["copy_mandates"][paused.owner]["state"]
        command = {
            "schema": "copy_mandate_control_v1",
            "owner": paused.owner,
            "mandate_sha": paused.sha,
            "predecessor_sha": digest(state),
            "event": "PAUSE",
            "issued_ms": s.now,
            "expires_ms": s.now + 1000,
            "nonce": digest("pause-second"),
        }
        s.guardian.control_copy_mandate(
            sign(command, s.authority, "copy-mandate-control-v1"), now_ms=s.now
        )
        result = decide(s)
        assert result["status"] == "REVIEWABLE"
        assert result["source_dispositions"][paused.owner] == "MANDATE_PAUSED"
        ids = reserve(s, result)
        assert len(ids) == 1
        assert paused.owner not in s.journal.snapshot()["intents"][ids[0]]["pod_changes"]


def test_journal_rejects_fee_drift_while_retaining_reserved_obligation(tmp_path, monkeypatch):
    with scenario(tmp_path) as s:
        result = decide(s)
        ids = reserve(s, result)
        s.guardian.dispatch(ids[0], s.now + 2)
        original = s.journal.commit

        def forged(kind, at_ms, state, **kwargs):
            if kind == "RECONCILE":
                owner = next(iter(state["pod_fees_paid"]))
                state["pod_fees_paid"][owner] = "0"
            return original(kind, at_ms, state, **kwargs)

        monkeypatch.setattr(s.journal, "commit", forged)
        with pytest.raises(ValueError, match="POD_LEDGER_CONSERVATION"):
            s.guardian.reconcile(s.now + 3)
        state = s.journal.snapshot()
        assert state["reservations"]
        assert not state["fills"]
        assert len(s.broker.audit()) == 1


@pytest.mark.parametrize("fault", ["unknown", "hedge"])
def test_unverified_execution_mode_cannot_admit_opening(tmp_path, fault):
    with scenario(tmp_path) as s:
        s.broker.execution_position_mode = "HEDGE" if fault == "hedge" else "UNKNOWN"
        result = decide(s)
        assert result["status"] != "REVIEWABLE"
        assert not s.journal.snapshot()["intents"]
        assert not s.broker.audit()
