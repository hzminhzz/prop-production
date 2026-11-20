from decimal import Decimal

from test_guardian import NOW, reserve, review, setup_system


def test_cross_pod_nets_have_zero_broker_orders_and_durable_attribution(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path, opposed=True)
    receipt = review(guardian, proposal, NOW)
    assert receipt["status"] == "REVIEWABLE"
    assert reserve(guardian, proposal, receipt["decision_id"], NOW) == []
    state = journal.snapshot()
    assert Decimal(state["pod_positions"][proposal.mandates[0].owner]["BTC"]) > 0
    assert (
        Decimal(state["pod_positions"][proposal.mandates[0].owner]["BTC"])
        + Decimal(state["pod_positions"][proposal.mandates[1].owner]["BTC"])
        == 0
    )
    assert len(state["checkpoint_internal_effects"]) == 1
    assert broker.audit() == []
    before = journal.sequence
    assert reserve(guardian, proposal, receipt["decision_id"], NOW + 1) == []
    assert journal.sequence == before
    broker.mark("BTC", "9000", NOW + 2)
    guardian.reconcile(NOW + 2)
    assert Decimal(journal.snapshot()["pod_loss_used"][proposal.mandates[0].owner]) > 0
    assert journal.snapshot()["finance"]["equity"] == "10000"
    journal.close()
    from prop_production.journal import Journal

    # Cross allocation is journaled once, and replay survives a disarmed restart.
    with Journal(tmp_path / "guardian.sqlite3", "fixture-account") as restarted:
        recovered = restarted.snapshot()
        assert recovered["pod_positions"] == state["pod_positions"]
        assert recovered["checkpoint_internal_effects"] == state["checkpoint_internal_effects"]
        assert recovered["runtime"]["permit"] is None
    broker.close()
    floor.close()


def test_opposing_pods_partial_fill_conserves_broker_fee_and_inventory(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(
        tmp_path, opposed=True, second_qty="-10"
    )
    try:
        receipt = review(guardian, proposal, NOW)
        assert receipt["status"] == "REVIEWABLE"
        ids = reserve(guardian, proposal, receipt["decision_id"], NOW)
        assert len(ids) == 1
        broker.fill_on_submit = False
        guardian.dispatch(ids[0], NOW)
        broker.fill(ids[0], "0.5")
        guardian.reconcile(NOW + 1)
        state = journal.snapshot()
        assert state["intents"][ids[0]]["status"] == "PARTIAL"
        total_pod_quantity = sum(
            (Decimal(p["BTC"]) for p in state["pod_positions"].values()), Decimal("0")
        )
        assert total_pod_quantity == Decimal(state["positions"]["BTC"])
        total_pod_fees = sum(map(Decimal, state["pod_fees_paid"].values()), Decimal("0"))
        broker_fees = sum(
            (Decimal(item["fee"]) for item in broker.ledger() if item["kind"] == "FILL"),
            Decimal("0"),
        )
        assert total_pod_fees == broker_fees
        assert "POD_FILL_ALLOCATION_INVALID" not in state["halts"]
    finally:
        journal.close()
        broker.close()
        floor.close()


def test_unattributed_funding_halts_instead_of_charging_every_pod(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path, opposed=True)
    try:
        receipt = review(guardian, proposal, NOW)
        assert reserve(guardian, proposal, receipt["decision_id"], NOW) == []
        before = (
            journal.snapshot()["pod_loss_used"] if "pod_loss_used" in journal.snapshot() else {}
        )
        broker.funding("-1", NOW + 1)
        result = guardian.reconcile(NOW + 1)
        state = journal.snapshot()
        assert result["certainty"] == "UNKNOWN"
        assert "BROKER_CASH_ATTRIBUTION_UNKNOWN" in state["halts"]
        assert state["finance"]["cash"] == "9999"
        assert state["pod_loss_used"] == before or all(
            Decimal(v) == Decimal(before.get(k, "0")) for k, v in state["pod_loss_used"].items()
        )
        assert broker.audit() == []
    finally:
        journal.close()
        broker.close()
        floor.close()


def test_verified_funding_conserves_opposed_pod_cash_and_mismatched_quantity_halts(tmp_path):
    from dataclasses import replace

    import pytest

    from prop_production.checkpoint_accounting import apply_funding
    from prop_production.gateway import FundingEvent

    shares = apply_funding(
        {"pod_positions": {"pod-a": {"BTC": "1.5"}, "pod-b": {"BTC": "-1.0"}}},
        {
            "funding_id": "exact-8h",
            "market": "BTC",
            "amount": "-1",
            "position_quantity": "0.5",
            "known_at_ms": NOW,
        },
        expected_quantity=Decimal("0.5"),
    )
    assert shares == {"pod-a": "-3", "pod-b": "2"}

    guardian, broker, journal, proposal, floor = setup_system(
        tmp_path, opposed=True, second_qty="-10"
    )
    try:
        receipt = review(guardian, proposal, NOW)
        ids = reserve(guardian, proposal, receipt["decision_id"], NOW)
        guardian.dispatch(ids[0], NOW)
        guardian.reconcile(NOW + 1)

        before = journal.snapshot()
        pod_a_qty = Decimal(before["pod_positions"][proposal.mandates[0].owner]["BTC"])
        pod_b_qty = Decimal(before["pod_positions"][proposal.mandates[1].owner]["BTC"])
        net_qty = Decimal(before["positions"]["BTC"])
        assert pod_a_qty > 0 and pod_b_qty < 0
        assert pod_a_qty + pod_b_qty == net_qty
        assert "funding_events" not in before
        assert "pod_funding" not in before
        assert "checkpoint_funding_allocations" not in before
        cash_before = Decimal(before["finance"]["cash"])
        used_before = dict(before.get("pod_loss_used", {}))

        broker.funding("-1", NOW + 2)
        event = FundingEvent(
            funding_id="fund-8h-1",
            market="BTC",
            amount=Decimal("-1"),
            position_quantity=net_qty,
            known_at_ms=NOW + 2,
        )
        snap = replace(broker.observe(NOW + 2), funding_events=(event,))
        result = guardian.apply_snapshot(snap, NOW + 2)
        state = journal.snapshot()
        assert result["certainty"] == "COHERENT"
        assert not state["halts"]
        assert Decimal(state["finance"]["cash"]) == cash_before - Decimal("1")
        share_a = Decimal(state["pod_funding"][proposal.mandates[0].owner])
        share_b = Decimal(state["pod_funding"][proposal.mandates[1].owner])
        assert share_a < Decimal("-1")
        assert share_b > Decimal("0")
        assert share_a + share_b == Decimal("-1")
        assert (
            Decimal(state["pod_loss_used"][proposal.mandates[0].owner])
            == Decimal(used_before.get(proposal.mandates[0].owner, "0")) - share_a
        )
        assert Decimal(state["pod_loss_used"][proposal.mandates[1].owner]) == Decimal(
            used_before.get(proposal.mandates[1].owner, "0")
        )
        assert "fund-8h-1" in state["checkpoint_funding_allocations"]
        assert "fund-8h-1" in state["funding_events"]

        # Re-observing the same FundingEvent is idempotent and does not double-charge.
        repolled = guardian.apply_snapshot(
            replace(broker.observe(NOW + 3), funding_events=(event,)), NOW + 3
        )
        assert repolled["certainty"] == "COHERENT"
        assert journal.snapshot()["pod_funding"] == state["pod_funding"]

        # Position quantity mismatch on a new FundingEvent must HALT fail-closed.
        bad_event = FundingEvent(
            funding_id="fund-8h-2",
            market="BTC",
            amount=Decimal("-1"),
            position_quantity=net_qty - Decimal("0.01"),
            known_at_ms=NOW + 4,
        )
        halted = guardian.apply_snapshot(
            replace(broker.observe(NOW + 4), funding_events=(bad_event,)), NOW + 4
        )
        assert halted["certainty"] == "UNKNOWN"
        assert "FUNDING_POSITION_MISMATCH" in journal.snapshot()["halts"]

        # Tampering funding_events across HALT or rewrite must fail journal validation.
        tampered = journal.snapshot()
        tampered["funding_events"]["fund-8h-1"]["amount"] = "0"
        with pytest.raises(
            ValueError, match="FUNDING_HISTORY_REWRITTEN|ATTRIBUTION_HISTORY_REWRITE"
        ):
            journal.commit("HALT_TEST", NOW + 5, tampered, expected_sequence=journal.sequence)
        journal.verify()
    finally:
        journal.close()
        broker.close()
        floor.close()


def test_guardian_dispatch_accepts_ioc_cancelled_and_expired_without_unknown_halt(tmp_path):
    from prop_production.gateway import Outcome

    for ack_status, reason in (
        ("CANCELLED", "IOC_PARTIAL_FILL:0.1"),
        ("EXPIRED", "ORDER_EXPIRED_ZERO_FILL"),
    ):
        sub = tmp_path / ack_status
        sub.mkdir(mode=0o700)
        guardian, broker, journal, proposal, floor = setup_system(sub)
        try:
            receipt = review(guardian, proposal, NOW)
            ids = reserve(guardian, proposal, receipt["decision_id"], NOW)
            broker.fill_on_submit = False
            orig_submit = broker.submit

            def _submit(intent: dict, *, _orig=orig_submit, _st=ack_status, _rsn=reason) -> Outcome:
                out = _orig(intent)
                return Outcome(status=_st, broker_id=out.broker_id, reason=_rsn)

            guardian.gateway.submit = _submit  # type: ignore[method-assign]
            guardian.dispatch(ids[0], NOW)
            state = journal.snapshot()
            assert state["intents"][ids[0]]["status"] == "ACKNOWLEDGED"
            assert "UNKNOWN_OUTCOME" not in state["halts"]
        finally:
            journal.close()
            broker.close()
            floor.close()


def test_combined_checkpoint_fill_and_funding_event_in_single_reconcile(tmp_path):
    from dataclasses import replace

    from prop_production.gateway import FundingEvent

    guardian, broker, journal, proposal, floor = setup_system(
        tmp_path, opposed=True, second_qty="-10"
    )
    try:
        receipt = review(guardian, proposal, NOW)
        ids = reserve(guardian, proposal, receipt["decision_id"], NOW)
        broker.fill_on_submit = False
        guardian.dispatch(ids[0], NOW)
        broker.fill(ids[0], "1")
        fill_obs = broker.observe(NOW + 1)
        net_qty = fill_obs.positions[0].quantity
        broker.funding("-1", NOW + 2)
        event = FundingEvent(
            funding_id="fund-combo-checkpoint",
            market="BTC",
            amount=Decimal("-1"),
            position_quantity=net_qty,
            known_at_ms=NOW + 2,
        )
        snap = replace(broker.observe(NOW + 2), funding_events=(event,))
        result = guardian.apply_snapshot(snap, NOW + 2)
        state = journal.snapshot()
        assert result["certainty"] == "COHERENT"
        assert not state["halts"]
        assert Decimal(state["positions"]["BTC"]) == net_qty
        assert Decimal(state["pod_funding"][proposal.mandates[0].owner]) + Decimal(
            state["pod_funding"][proposal.mandates[1].owner]
        ) == Decimal("-1")
        assert "fund-combo-checkpoint" in state["checkpoint_funding_allocations"]
        assert journal.verify()["semantic_status"] == "VERIFIED_V2"
    finally:
        journal.close()
        broker.close()
        floor.close()


def test_funding_before_closing_fill_in_single_reconcile_stays_coherent(tmp_path):
    from dataclasses import replace

    from checkpoint_scenarios import scenario
    from test_checkpoint_netting import reserve
    from test_checkpoint_reversal import next_decision, opened

    from prop_production.gateway import FundingEvent

    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        open_qty = Decimal(s.journal.snapshot()["positions"]["BTC"])
        assert open_qty == Decimal("0.075")
        s.wave(2)
        s.broker.cross_zero_supported = False
        decision = next_decision(s, 3)
        s.broker.fill_on_submit = False
        ids = reserve(s, decision)
        s.guardian.dispatch(ids[0], s.now + 2)

        t1 = s.now + 3
        s.broker.funding("-1.5", t1)
        event = FundingEvent(
            funding_id="fund-pre-close",
            market="BTC",
            amount=Decimal("-1.5"),
            position_quantity=open_qty,
            known_at_ms=t1,
        )
        t2 = s.now + 5
        s.broker.mark("BTC", "10000", t2)
        s.broker.fill(ids[0], "1")
        snap = replace(s.broker.observe(t2 + 1), funding_events=(event,))
        result = s.guardian.apply_snapshot(snap, t2 + 1)
        state = s.journal.snapshot()
        assert result["certainty"] == "COHERENT"
        assert not state["halts"]
        assert Decimal(state["positions"]["BTC"]) == Decimal("0")
        assert sum(map(Decimal, state["pod_funding"].values()), Decimal("0")) == Decimal("-1.5")
        assert "fund-pre-close" in state["checkpoint_funding_allocations"]
        assert s.journal.verify()["semantic_status"] == "VERIFIED_V2"


def test_malformed_funding_events_shape_raises_value_error(tmp_path):
    import pytest

    from prop_production.checkpoint_accounting import validate_attribution
    from prop_production.journal import Journal

    with Journal(tmp_path / "malformed.sqlite", "fixture-account", initialize=True) as journal:
        base = journal.snapshot()
        bad_shapes = [
            ["not-a-dict"],
            {"f1": {"bad": 1}},
            {
                "f1": {
                    "funding_id": "f2",
                    "market": "BTC",
                    "amount": "-1",
                    "position_quantity": "1",
                    "known_at_ms": 100,
                }
            },
            {
                "f1": {
                    "funding_id": "f1",
                    "market": "BTC",
                    "amount": "-1",
                    "position_quantity": "0",
                    "known_at_ms": 100,
                }
            },
            {
                "f1": {
                    "funding_id": "f1",
                    "market": "BTC",
                    "amount": "NaN",
                    "position_quantity": "1",
                    "known_at_ms": 100,
                }
            },
            {
                "f1": {
                    "funding_id": "f1",
                    "market": "BTC",
                    "amount": "-1",
                    "position_quantity": "1",
                    "known_at_ms": True,
                }
            },
        ]
        for bad in bad_shapes:
            tampered = journal.snapshot()
            tampered["funding_events"] = bad
            with pytest.raises(ValueError, match="FUNDING_EVENT_INVALID"):
                validate_attribution(base, tampered, "RECONCILE")
            with pytest.raises(ValueError, match="FUNDING_EVENT_INVALID"):
                journal.commit("RECONCILE", 100, tampered, expected_sequence=journal.sequence)
