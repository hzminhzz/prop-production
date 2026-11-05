from decimal import Decimal

from test_guardian import setup_system
from test_planner import quote


def test_pod_adverse_spend_persists_and_blocks_reusing_the_same_budget(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    receipt = guardian.decide(proposal, quote(), 301000)
    ids = guardian.reserve(receipt.data["decision_id"], 301000)
    guardian.dispatch(ids[0], 301000)
    guardian.reconcile(301001)
    assert Decimal(journal.snapshot()["pod_loss_used"]["pod-a"]) > 0
    broker.mark("BTC", "9500", 301002)
    guardian.reconcile(301002)
    lost = Decimal(journal.snapshot()["pod_loss_used"]["pod-a"])
    broker.mark("BTC", "10000", 301003)
    guardian.reconcile(301003)
    assert Decimal(journal.snapshot()["pod_loss_used"]["pod-a"]) >= lost
    journal.close()
    broker.close()
    floor.close()


def test_existing_pod_loss_spend_reduces_new_feasible_scale():
    from test_risk import inputs

    from prop_production.risk import size_book
    args = list(inputs())
    first = size_book(*args, {}, {}, [], now_ms=1000)
    args[4]["pod_loss_used"] = {"pod-a": "150"}
    second = size_book(*args, {}, {}, [], now_ms=1000)
    assert Decimal(second["scale"]) < Decimal(first["scale"])


def test_invalid_snapshot_clears_flat_and_permit_instead_of_raising_with_old_authority(tmp_path):
    from dataclasses import replace
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    bad = replace(broker.observe(301000), equity="not-a-decimal")
    result = guardian.apply_snapshot(bad, 301000)
    assert result["certainty"] == "UNKNOWN"
    assert result["flat_confirmed"] is False
    assert journal.snapshot()["runtime"]["permit"] is None
    journal.close()
    broker.close()
    floor.close()
