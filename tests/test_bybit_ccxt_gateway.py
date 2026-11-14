"""Adversarial end-to-end tests for CCXT Bybit Demo reconciliation and BybitDemoGateway."""

from __future__ import annotations

import sys
from collections import deque
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from prop_production.bybit_gateway import (
    BybitDemoGateway,
    bybit_demo_account_binding,
    snapshot_from_bybit_reconciliation,
)


@pytest.fixture(scope="module", autouse=True)
def _isolate_connector_sys_modules() -> Any:
    """Keep prop_production's own CLI module-isolation test clean when running the full suite."""
    before = {name for name in sys.modules if name == "connectors" or name.startswith("connectors.")}
    yield
    for name in list(sys.modules):
        if (name == "connectors" or name.startswith("connectors.")) and name not in before:
            sys.modules.pop(name, None)


def _connector_symbols() -> tuple[Any, Any, Any, Any, Any, Any]:
    pytest.importorskip("connectors.bybit.reconcile")
    from connectors.bybit.client import BybitClient
    from connectors.bybit.config import BybitConfig, Environment, Mode
    from connectors.bybit.models import BybitInvalidOrderError, BybitNetworkError
    from connectors.bybit.reconcile import BybitDemoReconciler

    return (
        BybitClient,
        BybitConfig,
        Environment,
        Mode,
        BybitInvalidOrderError,
        (BybitNetworkError, BybitDemoReconciler),
    )


def _demo_config(*, mode: Any = None) -> Any:
    _, BybitConfig, Environment, Mode, _, _ = _connector_symbols()
    return BybitConfig(
        api_key="demo-key",
        api_secret="demo-secret",
        environment=Environment.DEMO,
        mode=Mode.READ_ONLY if mode is None else mode,
        expected_demo_uid="778899",
    )


def _mock_rest_exchange(*, empty_books: bool = False) -> AsyncMock:
    ex = AsyncMock()
    ex.hostname = "bybit.com"
    ex.urls = {"api": {"private": "https://api-demo.{hostname}"}}
    ex.isSandboxModeEnabled = False
    ex.options = {"enableDemoTrading": True}
    ex.enable_demo_trading = lambda enabled=True: None
    ex.load_markets = AsyncMock(return_value={})
    ex.close = AsyncMock(return_value=None)
    ex.fetch_time = AsyncMock(return_value=1_700_000_005_000)
    ex.market = lambda native_symbol: {
        "BTCUSDT": {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "linear": True, "swap": True, "settle": "USDT"},
    }[native_symbol]
    ex.private_get_v5_user_query_api = AsyncMock(
        return_value={"retCode": 0, "result": {"userID": "778899", "apiKey": "demo-key", "uta": "1", "readOnly": "0"}}
    )
    ex.private_get_v5_account_info = AsyncMock(
        return_value={"retCode": 0, "result": {"unifiedMarginStatus": 6}}
    )
    if empty_books:
        ex.fetch_balance = AsyncMock(
            return_value={
                "info": {
                    "result": {
                        "list": [
                            {
                                "accountType": "UNIFIED",
                                "totalEquity": "10000.00",
                                "totalAvailableBalance": "10000.00",
                                "totalWalletBalance": "10000.00",
                                "totalPerpUPL": "0",
                                "totalInitialMargin": "0",
                                "totalMaintenanceMargin": "0",
                            }
                        ]
                    }
                }
            }
        )
        ex.fetch_positions = AsyncMock(return_value=[])
        ex.fetch_open_orders = AsyncMock(return_value=[])
        ex.fetch_canceled_and_closed_orders = AsyncMock(return_value=[])
        ex.fetch_my_trades = AsyncMock(return_value=[])
        ex.fetch_ticker = AsyncMock(
            return_value={"last": 50000.0, "info": {"markPrice": "50000.0"}, "timestamp": 1_700_000_005_000}
        )
        return ex

    ex.fetch_balance = AsyncMock(
        return_value={
            "info": {
                "result": {
                    "list": [
                        {
                            "accountType": "UNIFIED",
                            "totalEquity": "10025.50",
                            "totalAvailableBalance": "9000.50",
                            "totalWalletBalance": "10000.00",
                            "totalPerpUPL": "25.50",
                            "totalInitialMargin": "1000.00",
                            "totalMaintenanceMargin": "50.00",
                        }
                    ]
                }
            }
        }
    )

    async def _fetch_positions(*, params: dict[str, Any]) -> list[dict[str, Any]]:
        if params.get("subType") == "linear" and params.get("settleCoin") == "USDT":
            return [
                {
                    "symbol": "BTC/USDT:USDT",
                    "contracts": 0.1,
                    "side": "long",
                    "entryPrice": 50000.0,
                    "markPrice": 50255.0,
                    "info": {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "0.1",
                        "avgPrice": "50000.0",
                        "markPrice": "50255.0",
                        "unrealisedPnl": "25.50",
                        "positionIdx": "0",
                    },
                }
            ]
        return []

    ex.fetch_positions = AsyncMock(side_effect=_fetch_positions)

    async def _fetch_open_orders(*, params: dict[str, Any]) -> list[dict[str, Any]]:
        if params.get("subType") == "linear" and params.get("settleCoin") == "USDT":
            return [
                {
                    "id": "ord-open-1",
                    "clientOrderId": "intent-open-1",
                    "symbol": "BTC/USDT:USDT",
                    "side": "sell",
                    "type": "limit",
                    "price": 52000.0,
                    "amount": 0.1,
                    "filled": 0.0,
                    "status": "open",
                    "reduceOnly": True,
                    "timestamp": 1_700_000_002_000,
                    "info": {
                        "orderId": "ord-open-1",
                        "orderLinkId": "intent-open-1",
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "orderType": "Limit",
                        "price": "52000.0",
                        "qty": "0.1",
                        "cumExecQty": "0",
                        "orderStatus": "New",
                        "reduceOnly": True,
                        "positionIdx": "0",
                    },
                }
            ]
        return []

    async def _fetch_closed_orders(*, since: int | None = None, params: dict[str, Any]) -> list[dict[str, Any]]:
        if params.get("subType") == "linear" and params.get("settleCoin") == "USDT":
            return [
                {
                    "id": "ord-closed-1",
                    "clientOrderId": "intent-entry-1",
                    "symbol": "BTC/USDT:USDT",
                    "side": "buy",
                    "type": "market",
                    "amount": 0.1,
                    "filled": 0.1,
                    "status": "closed",
                    "reduceOnly": False,
                    "timestamp": 1_700_000_001_000,
                    "info": {
                        "orderId": "ord-closed-1",
                        "orderLinkId": "intent-entry-1",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "orderType": "Market",
                        "qty": "0.1",
                        "cumExecQty": "0.1",
                        "orderStatus": "Filled",
                        "reduceOnly": False,
                        "positionIdx": "0",
                    },
                }
            ]
        return []

    ex.fetch_open_orders = AsyncMock(side_effect=_fetch_open_orders)
    ex.fetch_canceled_and_closed_orders = AsyncMock(side_effect=_fetch_closed_orders)

    async def _fetch_my_trades(*, symbol: str | None = None, since: int | None = None, params: dict[str, Any]) -> list[dict[str, Any]]:
        if params.get("settleCoin") == "USDT":
            return [
                {
                    "id": "exec-1",
                    "order": "ord-closed-1",
                    "symbol": "BTC/USDT:USDT",
                    "side": "buy",
                    "amount": 0.1,
                    "price": 50000.0,
                    "fee": {"cost": 2.75, "currency": "USDT"},
                    "timestamp": 1_700_000_001_100,
                    "info": {
                        "execId": "exec-1",
                        "orderId": "ord-closed-1",
                        "orderLinkId": "intent-entry-1",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "execQty": "0.1",
                        "execPrice": "50000.0",
                        "execFee": "2.75",
                        "execTime": "1700000001100",
                    },
                }
            ]
        return []

    ex.fetch_my_trades = AsyncMock(side_effect=_fetch_my_trades)
    return ex


def _mock_ws_exchange() -> AsyncMock:
    import asyncio

    async def _idle_watch(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(0.05)
        return {}

    async def _idle_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        await asyncio.sleep(0.05)
        return []

    ws = AsyncMock()
    ws.hostname = "bybit.com"
    ws.urls = {"api": {"ws": {"private": {"contract": "wss://stream-demo.{hostname}/v5/private"}}}}
    ws.isSandboxModeEnabled = False
    ws.options = {"enableDemoTrading": True}
    ws.watch_balance = AsyncMock(side_effect=_idle_watch)
    ws.watch_positions = AsyncMock(side_effect=_idle_list)
    ws.watch_orders = AsyncMock(side_effect=_idle_list)
    ws.watch_my_trades = AsyncMock(side_effect=_idle_list)
    ws.watch_ticker = AsyncMock(side_effect=_idle_watch)
    ws.close = AsyncMock(return_value=None)
    return ws


def test_ccxt_snapshot_is_guardian_ready_and_fails_closed_on_drift() -> None:
    import asyncio

    BybitClient, _, _, _, _, (_, BybitDemoReconciler) = _connector_symbols()
    ex = _mock_rest_exchange()
    ws = _mock_ws_exchange()
    client = BybitClient(_demo_config(), exchange=ex)
    clock = deque([1_700_000_005_100])
    reconciler = BybitDemoReconciler(
        client, ws_exchange=ws, symbols=("BTC/USDT:USDT",), now_ms_fn=lambda: clock.popleft()
    )
    reconciled = asyncio.run(reconciler.catch_up())
    snap = snapshot_from_bybit_reconciliation(reconciled, expected_demo_uid="778899")

    assert snap.ready(1_700_000_006_000, 5000) is True
    assert snap.cash == Decimal("10000.00")
    assert snap.unrealized == Decimal("25.50")
    assert snap.equity == Decimal("10025.50")
    assert snap.equity == snap.cash + snap.unrealized
    assert snap.initial_margin == Decimal("1000.00")
    assert snap.maintenance_margin == Decimal("50.00")
    assert snap.orders[0].reduce_only is True or snap.orders[1].reduce_only is True
    assert snap.fills[0].intent_id == "intent-entry-1"
    assert snap.fills[0].quantity == Decimal("0.1")

    # Stale timestamp fails closed (> 5000ms)
    assert snap.ready(1_700_000_010_500, 5000) is False

    # Conservation mismatch fails closed even if connected=True
    broken = type(reconciled)(
        **{
            **reconciled.__dict__,
            "account": type(reconciled.account)(
                equity_usd=Decimal("9999.00"),
                available_usd=reconciled.account.available_usd,
                wallet_balance_usd=reconciled.account.wallet_balance_usd,
                unrealized_pnl_usd=reconciled.account.unrealized_pnl_usd,
                initial_margin_usd=reconciled.account.initial_margin_usd,
                maintenance_margin_usd=reconciled.account.maintenance_margin_usd,
            ),
        }
    )
    broken_snap = snapshot_from_bybit_reconciliation(broken, expected_demo_uid="778899")
    assert "UNIFIED_FINANCE_CONSERVATION_MISMATCH" in broken_snap.uncertainty_reasons
    assert broken_snap.ready(1_700_000_006_000, 5000) is False

    # Foreign order missing intent_id fails closed in ready()
    foreign_order = type(reconciled.orders[0])(
        **{**reconciled.orders[0].__dict__, "client_order_id": None}
    )
    foreign = type(reconciled)(**{**reconciled.__dict__, "orders": (foreign_order,)})
    foreign_snap = snapshot_from_bybit_reconciliation(foreign, expected_demo_uid="778899")
    assert foreign_snap.ready(1_700_000_006_000, 5000) is False


def test_bybit_demo_gateway_lifecycle_submit_cancel_and_non_retry_unknown() -> None:
    BybitClient, _, _, Mode, BybitInvalidOrderError, (BybitNetworkError, BybitDemoReconciler) = _connector_symbols()
    ex = _mock_rest_exchange()
    ws = _mock_ws_exchange()
    ro_client = BybitClient(_demo_config(mode=Mode.READ_ONLY), exchange=ex)
    ro_reconciler = BybitDemoReconciler(ro_client, ws_exchange=ws, now_ms_fn=lambda: 1_700_000_005_100)
    ro_gateway = BybitDemoGateway(ro_reconciler, expected_demo_uid="778899")
    with pytest.raises(PermissionError, match="REMOTE_MUTATION_UNAVAILABLE"):
        ro_gateway.submit({"intent_id": "i-1", "market": "BTC/USDT:USDT", "quantity": "0.1", "reduce_only": False})

    ex_exec = _mock_rest_exchange()
    ex_exec.create_order = AsyncMock(
        return_value={
            "id": "ord-new-1",
            "clientOrderId": "intent-42",
            "symbol": "BTC/USDT:USDT",
            "side": "sell",
            "type": "limit",
            "price": 51000.0,
            "amount": 0.05,
            "filled": 0.0,
            "status": "open",
            "reduceOnly": True,
            "timestamp": 1_700_000_005_500,
        }
    )
    ex_exec.cancel_order = AsyncMock(return_value={})
    exec_client = BybitClient(_demo_config(mode=Mode.EXECUTION), exchange=ex_exec)
    exec_reconciler = BybitDemoReconciler(exec_client, ws_exchange=_mock_ws_exchange(), now_ms_fn=lambda: 1_700_000_005_100)

    with BybitDemoGateway(exec_reconciler, expected_demo_uid="778899") as gateway:
        snap = gateway.observe(1_700_000_005_500)
        assert snap.ready(1_700_000_005_500, 5000) is True

        outcome = gateway.submit(
            {
                "intent_id": "intent-42",
                "market": "BTC/USDT:USDT",
                "quantity": "-0.05",
                "price": "51000.0",
                "reduce_only": True,
            }
        )
        assert outcome.status == "ACCEPTED"
        assert outcome.broker_id == "ord-new-1"
        assert ex_exec.create_order.call_args.kwargs["params"] == {
            "orderLinkId": "intent-42",
            "reduceOnly": True,
        }

        cancel_out = gateway.cancel("intent-42")
        assert cancel_out.status == "CANCELLED"
        assert cancel_out.broker_id == "ord-new-1"

        # Ambiguous network error MUST return UNKNOWN and NEVER retry
        ex_exec.create_order = AsyncMock(side_effect=BybitNetworkError("socket timeout"))
        ambiguous = gateway.submit(
            {"intent_id": "intent-43", "market": "BTC/USDT:USDT", "quantity": "0.02", "reduce_only": False}
        )
        assert ambiguous.status == "UNKNOWN"
        assert ex_exec.create_order.call_count == 1

        # Explicit exchange validation failure maps to REJECTED
        ex_exec.create_order = AsyncMock(side_effect=BybitInvalidOrderError("min qty"))
        rejected = gateway.submit(
            {"intent_id": "intent-44", "market": "BTC/USDT:USDT", "quantity": "0.0001", "reduce_only": False}
        )
        assert rejected.status == "REJECTED"


def test_guardian_reconciles_coherent_with_bybit_demo_gateway_and_halts_when_stale(tmp_path: Any) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from prop_production.evidence import digest, sign
    from prop_production.finance import Rulebook
    from prop_production.guardian import Guardian
    from prop_production.journal import Journal
    from prop_production.release import ReleaseFloor, ReleaseVerifier, fixture_release
    from prop_production.risk import fixture_markets, fixture_policy

    uid = "778899"
    binding = bybit_demo_account_binding(uid)
    key = Ed25519PrivateKey.generate()
    rules = Rulebook(
        binding,
        "SYNTHETIC",
        valid_until_ms=10**15,
        evidence_sha=digest("test-only bybit demo rulebook"),
    )
    policy, markets = fixture_policy(), fixture_markets()
    body = fixture_release()
    body["account_binding"] = binding
    body["risk_policy_sha"] = digest({"policy": policy, "markets": markets})
    body["rulebook_sha"] = rules.sha
    release = ReleaseVerifier(
        key.public_key(), "approver", binding, body["risk_policy_sha"], rules.sha
    ).verify(sign(body, key, "approver"), now_ms=1000)

    floor = ReleaseFloor(tmp_path / "release.sqlite", initialize=True)
    floor.accept(release)
    journal = Journal(tmp_path / "account.sqlite", binding, initialize=True)
    try:
        BybitClient, _, _, _, _, (_, BybitDemoReconciler) = _connector_symbols()
        ex = _mock_rest_exchange(empty_books=True)
        ws = _mock_ws_exchange()
        client = BybitClient(_demo_config(), exchange=ex)
        reconciler = BybitDemoReconciler(
            client,
            ws_exchange=ws,
            symbols=("BTC/USDT:USDT",),
            now_ms_fn=lambda: 1_700_000_005_100,
        )

        with BybitDemoGateway(reconciler, expected_demo_uid=uid) as gateway:
            guardian = Guardian(journal, release, floor, key.public_key(), rules, policy, markets, gateway)
            res = guardian.reconcile(1_700_000_006_000)
            assert res["certainty"] == "COHERENT"
            assert res["flat_confirmed"] is True

            # Advance > 5000ms without fresh broker snapshot -> fail-closed UNRESOLVED halt
            stale = guardian.reconcile(1_700_000_011_000)
            assert stale["state"] == "UNRESOLVED"
            assert stale["certainty"] == "UNKNOWN"
            assert stale["flat_confirmed"] is False
            assert "RECONCILIATION_UNCERTAIN" in journal.snapshot()["halts"]
    finally:
        journal.close()
        floor.close()
