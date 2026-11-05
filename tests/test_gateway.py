from decimal import Decimal

import pytest


def test_readonly_snapshot_has_exact_unknowns_and_rejects_mutation():
    from prop_production.gateway import ReadOnlyGateway, snapshot_from_wire

    raw = {"account_binding": "fixture-account", "endpoint": "https://fixture.invalid",
           "observed_at_ms": 1000, "received_at_ms": 1001, "epoch": 1,
           "cash": "10000", "equity": "10000", "unrealized": "0",
           "initial_margin": None, "maintenance_margin": None, "positions": [],
           "orders": [], "fills": [], "cutoff": None, "history_complete": False,
           "connected": True, "financial_complete": False}
    snap = snapshot_from_wire(raw)
    assert snap.equity == Decimal("10000")
    assert snap.ready(1002, 5000) is False
    gateway = ReadOnlyGateway(lambda: snap, "fixture-account", "https://fixture.invalid")
    assert gateway.observe(1002) == snap
    with pytest.raises(PermissionError):
        gateway.submit({"quantity": "1"})
    with pytest.raises(PermissionError):
        gateway.cancel("any")


def test_stale_disconnect_and_cross_account_never_ready():
    from prop_production.gateway import ReadOnlyGateway, fixture_snapshot

    fresh = fixture_snapshot(1000)
    assert fresh.ready(1001, 5000)
    assert not fresh.ready(9000, 5000)
    assert not fresh.ready(999, 5000)
    with pytest.raises(ValueError, match="ACCOUNT"):
        ReadOnlyGateway(lambda: fresh, "other", fresh.endpoint).observe(1001)
