"""Bybit Demo connector -> Guardian fail-closed adapter, with zero network mutation."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from prop_production.gateway import ReadOnlyGateway, inspect_bybit_demo_readonly

NOW = 1_795_000_000_000


class DemoClient:
    def __init__(self):
        self.config = SimpleNamespace(
            environment=SimpleNamespace(value="demo"),
            mode=SimpleNamespace(value="read_only"),
            expected_demo_uid="fixture-24680",
        )
        self.demo_identity_verified = True
        self.demo_identity_evidence = {
            "environment": "demo", "private_rest": "api-demo.bybit.com",
            "authenticated_uid_matches_expected": True,
            "dedicated_demo_key_matches": True,
            "unified_account_verified": True,
            "key_is_read_only": True,
        }
        self.calls = []

    async def fetch_demo_account_summary(self):
        self.calls.append("wallet_GET")
        return SimpleNamespace(equity_usd=Decimal("5000.123456789"),
                               available_usd=Decimal("4500.000000001"))

    async def fetch_demo_positions(self):
        self.calls.append("positions_GET")
        return [SimpleNamespace(
            symbol="BTC/USDT:USDT", side="buy", size=Decimal("0.012345678"),
            entry_price=Decimal("73500"), mark_price=Decimal("73501"),
        )]

    async def fetch_demo_open_orders(self):
        self.calls.append("orders_GET")
        return [SimpleNamespace(order_id="fixture-order-1", client_order_id=None,
                                symbol="ETH/USDT:USDT", side="sell",
                                quantity=Decimal("1.00000000001"),
                                filled_quantity=Decimal("0.25000000001"),
                                status="partially_filled")]


def run(client):
    return asyncio.run(inspect_bybit_demo_readonly(client, NOW))


def test_demo_scoped_native_evidence_keeps_decimal_and_unknowns():
    client = DemoClient()
    snap = run(client)
    assert snap.endpoint == "https://api-demo.bybit.com"
    assert snap.account_binding != "fixture-24680"
    assert len(snap.account_binding) == 64
    assert snap.equity == Decimal("5000.123456789")
    assert snap.provider_buying_power == Decimal("4500.000000001")
    assert snap.cash is None and snap.provider_balance is None
    assert snap.unrealized is None and snap.initial_margin is None
    assert snap.positions[0].quantity == Decimal("0.012345678")
    assert snap.orders[0].quantity == Decimal("-1.00000000001")
    assert snap.orders[0].filled == Decimal("-0.25000000001")
    assert snap.orders[0].intent_id == ""  # never mint a Guardian intent
    assert snap.fills == () and not snap.native_inventory_read_complete
    assert snap.history_complete is False and snap.cutoff is None
    assert snap.connected is False and snap.financial_complete is False
    assert snap.ready(NOW, 10000) is False
    assert snap.evidence_schema == "bybit_demo_snapshot_v1"
    assert ("cash", "UNKNOWN") in snap.provider_financial_provenance
    assert "BYBIT_NO_VERIFIED_HISTORY_CUTOFF" in snap.uncertainty_reasons
    assert client.calls == ["wallet_GET", "positions_GET", "orders_GET"]
    read_only = ReadOnlyGateway(lambda: snap, snap.account_binding, snap.endpoint)
    assert read_only.observe(NOW) is snap
    with pytest.raises(PermissionError, match="REMOTE_MUTATION_UNAVAILABLE"):
        read_only.submit({"intent_id": "fixture"})
    with pytest.raises(PermissionError, match="REMOTE_MUTATION_UNAVAILABLE"):
        read_only.cancel("fixture")


@pytest.mark.parametrize("change", [
    "environment", "mode", "uid", "identity", "endpoint", "not_connected",
])
def test_demo_identity_must_be_explicit_and_authenticated(change):
    c = DemoClient()
    if change == "environment":
        c.config.environment = SimpleNamespace(value="mainnet")
    elif change == "mode":
        c.config.mode = SimpleNamespace(value="execution")
    elif change == "uid":
        c.config.expected_demo_uid = ""
    elif change == "identity":
        c.demo_identity_evidence["authenticated_uid_matches_expected"] = False
    elif change == "endpoint":
        c.demo_identity_evidence["private_rest"] = "api.bybit.com"
    elif change == "not_connected":
        c.demo_identity_verified = False
    with pytest.raises(PermissionError, match="BYBIT_DEMO_SCOPE_UNVERIFIED"):
        run(c)
    assert c.calls == []


@pytest.mark.parametrize("field", ["equity_usd", "available_usd"])
def test_invalid_money_never_becomes_guardian_cash(field):
    c = DemoClient()
    original = c.fetch_demo_account_summary

    async def bad():
        result = await original()
        setattr(result, field, Decimal("NaN"))
        return result

    c.fetch_demo_account_summary = bad
    with pytest.raises(ValueError):
        run(c)


def test_unsupported_hedge_and_duplicate_inventory_fail_closed():
    c = DemoClient()

    async def duplicate_positions():
        row = (await DemoClient().fetch_demo_positions())[0]
        return [row, row]

    c.fetch_demo_positions = duplicate_positions
    with pytest.raises(ValueError, match="BYBIT_POSITION_EVIDENCE_INVALID"):
        run(c)


def test_unsupported_order_fields_fail_closed():
    c = DemoClient()

    async def invalid_orders():
        return [SimpleNamespace(order_id="o", client_order_id=None,
                                symbol="BTC/USDT:USDT", side="sell",
                                quantity=Decimal("1"), filled_quantity=Decimal("2"),
                                status="partially_filled")]

    c.fetch_demo_open_orders = invalid_orders
    with pytest.raises(ValueError, match="BYBIT_ORDER_EVIDENCE_INVALID"):
        run(c)
