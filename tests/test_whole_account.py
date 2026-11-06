from decimal import Decimal

from test_guardian import setup_system
from test_planner import quote


def test_cross_pod_nets_have_zero_broker_orders_and_durable_attribution(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path, opposed=True)
    receipt = guardian.decide(proposal, quote(), 301000)
    assert receipt.data["status"] == "PREVIEW"
    assert guardian.reserve(receipt.data["decision_id"], 301000) == []
    state = journal.snapshot()
    assert Decimal(state["pod_positions"]["pod-a"]["BTC"]) > 0
    assert Decimal(state["pod_positions"]["pod-a"]["BTC"]) + Decimal(state["pod_positions"]["pod-b"]["BTC"]) == 0
    assert len(state["internal_allocations"]) == 1
    assert state["internal_allocations"][0]["scope"] == "SYNTHETIC_ZERO_BROKER_NET"
    assert broker.audit() == []
    before = journal.sequence
    assert guardian.reserve(receipt.data["decision_id"], 301001) == []
    assert journal.sequence == before
    broker.mark("BTC", "9000", 301002)
    guardian.reconcile(301002)
    assert Decimal(journal.snapshot()["pod_loss_used"]["pod-a"]) > 0
    assert journal.snapshot()["finance"]["equity"] == "10000"
    journal.close()
    from prop_production.journal import Journal
    # Cross allocation is journaled once, and replay survives a disarmed restart.
    with Journal(tmp_path / "account.sqlite", "fixture-account") as restarted:
        recovered = restarted.snapshot()
        assert recovered["pod_positions"] == state["pod_positions"]
        assert recovered["internal_allocations"] == state["internal_allocations"]
        assert recovered["runtime"]["permit"] is None
    broker.close()
    floor.close()


def test_two_previews_cannot_spend_the_same_financial_epoch(tmp_path):
    import pytest

    from prop_production.contracts import TargetBatchProposal
    from prop_production.evidence import digest
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    first = guardian.decide(proposal, quote(), 301000)
    second_data = proposal.data
    second_data["decision_ms"] = 301001
    second_data["proposal_id"] = digest({k: v for k, v in second_data.items() if k != "proposal_id"})
    second = guardian.decide(TargetBatchProposal.from_dict(second_data), quote(), 301001)
    with pytest.raises(ValueError, match="RESERVATION_NOT_PERMITTED"):
        guardian.reserve(first.data["decision_id"], 301001)
    guardian.reserve(second.data["decision_id"], 301001)
    assert len(journal.snapshot()["reservations"]) == 1
    journal.close()
    broker.close()
    floor.close()
