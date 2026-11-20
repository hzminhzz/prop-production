from decimal import Decimal

from test_guardian import NOW, reserve, review, setup_system


def test_pod_adverse_spend_persists_and_blocks_reusing_the_same_budget(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    receipt = review(guardian, proposal, NOW)
    ids = reserve(guardian, proposal, receipt["decision_id"], NOW)
    guardian.dispatch(ids[0], NOW)
    guardian.reconcile(NOW + 1)
    assert Decimal(journal.snapshot()["pod_loss_used"][proposal.mandates[0].owner]) > 0
    broker.mark("BTC", "9500", NOW + 2)
    guardian.reconcile(NOW + 2)
    lost = Decimal(journal.snapshot()["pod_loss_used"][proposal.mandates[0].owner])
    broker.mark("BTC", "10000", NOW + 3)
    guardian.reconcile(NOW + 3)
    assert Decimal(journal.snapshot()["pod_loss_used"][proposal.mandates[0].owner]) >= lost
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
    bad = replace(broker.observe(NOW), equity="not-a-decimal")
    result = guardian.apply_snapshot(bad, NOW)
    assert result["certainty"] == "UNKNOWN"
    assert result["flat_confirmed"] is False
    assert journal.snapshot()["runtime"]["permit"] is None
    journal.close()
    broker.close()
    floor.close()
