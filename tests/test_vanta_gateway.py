"""Tests for VantaGateway and Vanta rulebook-driven CWSA execution."""

from __future__ import annotations

import inspect
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from prop_production.bybit_executor import CwsaExecutor
from prop_production.gateway import Outcome, Position
from prop_production.vanta_gateway import (
    VANTA_API_URL,
    VantaGateway,
    build_vanta_markets,
    vanta_account_binding,
)


def _write_cwsa_db(
    path: Path,
    *,
    seq: int = 1,
    next_cp: int = 1794683700000,
    positions_by_sid: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cwsa_heads (stream TEXT PRIMARY KEY, scope TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL, head TEXT NOT NULL)"
    )
    pos_map = positions_by_sid or {"ACT04": []}
    for idx, sid in enumerate(sorted(pos_map), start=1):
        state = {
            "sequence": seq,
            "next_checkpoint_ms": next_cp,
            "economic_sha": f"econ-{sid}-{seq}",
            "positions": pos_map[sid],
        }
        conn.execute(
            "INSERT INTO cwsa_heads VALUES (?, ?, ?, ?, ?)",
            (f"s{idx}", json.dumps({"source_id": sid}), "{}", json.dumps(state), f"head-{sid}"),
        )
    conn.commit()
    conn.close()


def test_build_vanta_markets_maps_hl_coins_and_ignores_disabled() -> None:
    payload = {
        "allowed": [
            {"tradePairId": "BTCUSDC", "hlCoin": "BTC"},
            {"tradePairId": "GOLDUSDC", "hlCoin": "xyz:GOLD"},
        ],
        "disabled": [{"tradePairId": "XAUUSD", "hlCoin": "XAU"}],
    }
    markets = build_vanta_markets(payload)
    assert set(markets) >= {"BTC", "xyz:GOLD"}
    assert markets["BTC"].symbol == "BTCUSDC"
    assert markets["xyz:GOLD"].symbol == "GOLDUSDC"
    assert "XAU" not in markets


def test_vanta_gateway_end_to_end_observe_execute_and_eliminated_block(tmp_path: Path) -> None:
    httpx = pytest.importorskip("httpx")
    vanta_mod = pytest.importorskip("connectors.vanta")
    VantaClient = vanta_mod.VantaClient
    VantaConfig = vanta_mod.VantaConfig
    VantaOrderClient = vanta_mod.VantaOrderClient

    account_id = "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f"
    cfg = VantaConfig(account_id=account_id, api_key="vk_test_id", api_secret="vks_test_secret")

    state: dict[str, Any] = {
        "eliminated": False,
        "stale": False,
        "balance": 5000.0,
        "equity": 5000.0,
        "open_pnl": 0.0,
        "positions": [],
        "orders": [],
        "posted": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/api/v1/keys/self":
                return httpx.Response(200, json={"success": True, "data": {"keyId": cfg.api_key, "accountId": account_id}})
            if path == "/api/v1/trading/trade-pairs":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "accountId": account_id,
                            "allowed": [{"tradePairId": "BTCUSDC", "hlCoin": "BTC"}],
                            "disabled": [],
                        },
                    },
                )
            if path == "/api/v1/trading/account":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "accountId": account_id,
                            "evaluation": {"isEliminated": state["eliminated"]},
                            "account": {
                                "currentBalance": state["balance"],
                                "currentEquity": state["equity"],
                                "openPnL": state["open_pnl"],
                                "capitalUsed": 0,
                            },
                            "meta": {"generatedAtMs": 1794683599000, "stale": state["stale"]},
                        },
                    },
                )
            if path == "/api/v1/trading/positions":
                return httpx.Response(200, json={"success": True, "data": state["positions"]})
            if path == "/api/v1/trading/orders":
                return httpx.Response(200, json={"success": True, "data": state["orders"]})
        elif request.method == "POST" and path == "/api/v1/trading/orders":
            body = json.loads(request.content)
            trade = body["trade"]
            state["posted"].append(trade)
            oid = f"ord-v-{len(state['posted'])}"
            if trade["execution_type"] == "MARKET":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "success": True,
                            "order_uuid": oid,
                            "order": {"orderUuid": oid, "filled": True, "quantity": trade["quantity"]},
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {"success": True}})
        raise AssertionError(f"Unexpected {request.method} {path}")

    transport = httpx.MockTransport(handler)
    client = VantaClient(cfg, transport=transport)
    order_client = VantaOrderClient(cfg, confirm_account_id=account_id, allow_execution=True, transport=transport)

    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    now_ms = 1794683600000
    _write_cwsa_db(source_db, seq=1, next_cp=now_ms, positions_by_sid={"ACT04": []})

    ex = CwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001")},
        rulebook_key="vanta-trading:classic_1step_5pct:phase_1",
        allow_rest_reconciliation=True,
        reset_time_hhmm="00:00",
    )

    with VantaGateway(client, account_id=account_id, endpoint=VANTA_API_URL, order_client=order_client) as gw:
        markets = gw.load_markets()
        snap = gw.observe(now_ms)
        assert snap.account_binding == vanta_account_binding(account_id)
        assert snap.equity == Decimal("5000.0")

        # Initial empty wave marks source baseline
        ex.step(gw, markets, now_ms, execute=True)

        # Wave 2 opens long BTC -> LONG MARKET sent to BTCUSDC
        _write_cwsa_db(
            source_db,
            seq=2,
            next_cp=now_ms + 300_000,
            positions_by_sid={"ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"}]},
        )
        res = ex.step(gw, markets, now_ms + 300_000, execute=True)
        assert res["status"] == "EXECUTED"
        assert res["submitted"] == 1
        assert state["posted"][-1]["order_type"] == "LONG"
        assert state["posted"][-1]["trade_pair"] == "BTCUSDC"
        assert state["posted"][-1]["quantity"] == 0.02

        # Eliminated flag immediately blocks further steps
        state["eliminated"] = True
        blocked = ex.step(gw, markets, now_ms + 305_000, execute=True)
        assert blocked["status"] == "BLOCKED"
        assert "VANTA_ACCOUNT_ELIMINATED" in blocked["reason"]

        # Cancel tracked order uses LIMIT_CANCEL
        cancel_out = gw.cancel(res["results"][0]["intent_id"])
        assert cancel_out.status == "CANCELLED"
        assert state["posted"][-1]["execution_type"] == "LIMIT_CANCEL"


def test_build_vanta_markets_ignores_vanta_source_hl_coin_collisions() -> None:
    payload = {
        "allowed": [
            {"tradePairId": "BTCUSDC", "source": "hyperliquid", "hlCoin": "BTC"},
            {"tradePairId": "SUIUSDC", "source": "hyperliquid", "hlCoin": "SUI"},
            {"tradePairId": "SUI", "source": "vanta", "hlCoin": "SUI"},
            {"tradePairId": "EURUSD", "source": "vanta", "hlCoin": "EUR"},
            {"tradePairId": "AUDCAD", "source": "vanta", "hlCoin": "AUD"},
        ],
        "disabled": [],
    }
    markets = build_vanta_markets(payload)
    assert markets["SUI"].symbol == "SUIUSDC"
    assert "EUR" not in markets
    assert "AUD" not in markets
    assert markets["EURUSD"].symbol == "EURUSD"
    assert markets["BTC"].min_amount == Decimal("1e-8")


def test_vanta_gateway_observes_short_positions_and_flags_sign_or_conservation_errors() -> None:
    httpx = pytest.importorskip("httpx")
    vanta_mod = pytest.importorskip("connectors.vanta")
    VantaClient = vanta_mod.VantaClient
    VantaConfig = vanta_mod.VantaConfig

    account_id = "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f"
    cfg = VantaConfig(account_id=account_id, api_key="vk_test_id", api_secret="vks_test_secret")
    now_ms = 1794683600000

    state: dict[str, Any] = {
        "account_size": 10000,
        "balance": 10000,
        "equity": 10000,
        "open_pnl": 0,
        "elimination": None,
        "positions": [
            {
                "positionUuid": "pos-short-1",
                "tradePair": "BTCUSDC",
                "positionType": "SHORT",
                "netQuantity": -0.25,
                "netValue": 15000,
                "averageEntryPrice": 60000,
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/api/v1/keys/self":
                return httpx.Response(200, json={"success": True, "data": {"keyId": cfg.api_key, "accountId": account_id}})
            if path == "/api/v1/trading/account":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "accountId": account_id,
                            "accountSize": state["account_size"],
                            "evaluation": {"isEliminated": False},
                            "elimination": state["elimination"],
                            "account": {
                                "currentBalance": state["balance"],
                                "currentEquity": state["equity"],
                                "openPnL": state["open_pnl"],
                                "capitalUsed": 0,
                            },
                            "meta": {"generatedAtMs": 1794683599000, "stale": False},
                        },
                    },
                )
            if path == "/api/v1/trading/positions":
                return httpx.Response(200, json={"success": True, "data": state["positions"]})
            if path == "/api/v1/trading/orders":
                return httpx.Response(200, json={"success": True, "data": []})
        raise AssertionError(f"Unexpected {request.method} {path}")

    transport = httpx.MockTransport(handler)
    client = VantaClient(cfg, transport=transport)
    with VantaGateway(client, account_id=account_id, expected_account_size=Decimal("10000")) as gw:
        snap = gw.observe(now_ms)
        assert snap.positions == (
            Position(
                market="BTCUSDC",
                quantity=Decimal("-0.25"),
                entry_price=Decimal("60000"),
                mark_price=Decimal("60000"),
            ),
        )
        assert snap.market_marks == (("BTCUSDC", Decimal("60000"), now_ms),)
        assert "VANTA_POSITION_INVALID" not in snap.uncertainty_reasons

        state["positions"] = [
            {
                "positionUuid": "pos-bad-1",
                "tradePair": "BTCUSDC",
                "positionType": "LONG",
                "netQuantity": -0.1,
                "netValue": 6000,
                "averageEntryPrice": 60000,
            }
        ]
        assert "VANTA_POSITION_INVALID" in gw.observe(now_ms).uncertainty_reasons

        state["positions"] = [
            {
                "positionUuid": "pos-bad-2",
                "tradePair": "BTCUSDC",
                "positionType": "SHORT",
                "netQuantity": 0.1,
                "netValue": 6000,
                "averageEntryPrice": 60000,
            }
        ]
        assert "VANTA_POSITION_INVALID" in gw.observe(now_ms).uncertainty_reasons

        state["positions"] = []
        state["equity"] = 9990
        assert "VANTA_FINANCE_CONSERVATION_MISMATCH" in gw.observe(now_ms).uncertainty_reasons

        state["equity"] = 10000
        state["elimination"] = {"reason": "MAX_DAILY_LOSS", "eliminatedAtMs": 1794683590000}
        assert "VANTA_ACCOUNT_ELIMINATED" in gw.observe(now_ms).uncertainty_reasons

    state["elimination"] = None
    client_mismatch = VantaClient(cfg, transport=transport)
    with VantaGateway(client_mismatch, account_id=account_id, expected_account_size=Decimal("5000")) as gw_mismatch:
        assert "VANTA_ACCOUNT_SIZE_MISMATCH" in gw_mismatch.observe(now_ms).uncertainty_reasons


def test_vanta_gateway_handles_transient_read_outages_and_classifies_submit_errors() -> None:
    httpx = pytest.importorskip("httpx")
    vanta_mod = pytest.importorskip("connectors.vanta")
    VantaClient = vanta_mod.VantaClient
    VantaConfig = vanta_mod.VantaConfig
    VantaOrderClient = vanta_mod.VantaOrderClient

    account_id = "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f"
    cfg = VantaConfig(account_id=account_id, api_key="vk_test_id", api_secret="vks_test_secret")
    now_ms = 1794683600000

    state: dict[str, Any] = {
        "read_mode": "503",
        "post_mode": "timeout",
        "post_calls": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/api/v1/keys/self":
                return httpx.Response(200, json={"success": True, "data": {"keyId": cfg.api_key, "accountId": account_id}})
            if path == "/api/v1/trading/account":
                if state["read_mode"] == "503":
                    return httpx.Response(503, json={"error": "validator_unavailable"})
                if state["read_mode"] == "timeout":
                    raise httpx.ReadTimeout("read timed out", request=request)
            if path in ("/api/v1/trading/positions", "/api/v1/trading/orders"):
                return httpx.Response(200, json={"success": True, "data": []})
        elif request.method == "POST" and path == "/api/v1/trading/orders":
            state["post_calls"] += 1
            mode = state["post_mode"]
            if mode == "timeout":
                raise httpx.ReadTimeout("submit timed out", request=request)
            if mode == "400":
                return httpx.Response(400, json={"error": "invalid_trade_pair"})
            if mode == "403":
                return httpx.Response(403, json={"error": "Daily loss limit exceeded"})
            if mode == "429":
                return httpx.Response(
                    429,
                    headers={"Retry-After": "60"},
                    json={"error": "too_many_failed_requests"},
                )
        raise AssertionError(f"Unexpected {request.method} {path}")

    transport = httpx.MockTransport(handler)
    client = VantaClient(cfg, transport=transport)
    order_client = VantaOrderClient(cfg, confirm_account_id=account_id, allow_execution=True, transport=transport)

    with VantaGateway(client, account_id=account_id, order_client=order_client) as gw:
        snap_503 = gw.observe(now_ms)
        assert snap_503.financial_complete is False
        assert snap_503.native_inventory_read_complete is False
        assert any(r.startswith("VANTA_READ_UNAVAILABLE:") for r in snap_503.uncertainty_reasons)

        state["read_mode"] = "timeout"
        snap_timeout = gw.observe(now_ms + 1_000)
        assert snap_timeout.financial_complete is False
        assert snap_timeout.native_inventory_read_complete is False
        assert any(r.startswith("VANTA_READ_UNAVAILABLE:") for r in snap_timeout.uncertainty_reasons)

        state["post_mode"] = "timeout"
        out_timeout = gw.submit({"intent_id": "i-timeout", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_timeout == Outcome(status="UNKNOWN", reason="SUBMIT_AMBIGUOUS:VantaTransportError")

        state["post_mode"] = "400"
        out_400 = gw.submit({"intent_id": "i-400", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_400.status == "REJECTED"

        state["post_mode"] = "403"
        out_403 = gw.submit({"intent_id": "i-403", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_403.status == "REJECTED"

        state["post_mode"] = "429"
        out_429 = gw.submit({"intent_id": "i-429", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_429.status == "REJECTED"

        calls_before = state["post_calls"]
        out_cooldown = gw.submit({"intent_id": "i-cooldown", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_cooldown == Outcome(status="REJECTED", reason="VANTA_COOLDOWN_ACTIVE")
        assert state["post_calls"] == calls_before


def test_vanta_gateway_uses_flat_on_full_close_and_persists_cancel_across_restart() -> None:
    httpx = pytest.importorskip("httpx")
    vanta_mod = pytest.importorskip("connectors.vanta")
    VantaClient = vanta_mod.VantaClient
    VantaConfig = vanta_mod.VantaConfig
    VantaOrderClient = vanta_mod.VantaOrderClient

    account_id = "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f"
    cfg = VantaConfig(account_id=account_id, api_key="vk_test_id", api_secret="vks_test_secret")
    now_ms = 1794683600000
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/api/v1/keys/self":
                return httpx.Response(200, json={"success": True, "data": {"keyId": cfg.api_key, "accountId": account_id}})
            if path == "/api/v1/trading/account":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "accountId": account_id,
                            "accountSize": 5000,
                            "evaluation": {"isEliminated": False},
                            "elimination": None,
                            "account": {
                                "currentBalance": 5000,
                                "currentEquity": 5000,
                                "openPnL": 0,
                                "capitalUsed": 1200,
                            },
                            "meta": {"generatedAtMs": 1794683599000, "stale": False},
                        },
                    },
                )
            if path == "/api/v1/trading/positions":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": [
                            {
                                "positionUuid": "pos-long-1",
                                "tradePair": "BTCUSDC",
                                "positionType": "LONG",
                                "netQuantity": 0.02,
                                "netValue": 1200,
                                "averageEntryPrice": 60000,
                            }
                        ],
                    },
                )
            if path == "/api/v1/trading/orders":
                return httpx.Response(200, json={"success": True, "data": []})
        elif request.method == "POST" and path == "/api/v1/trading/orders":
            trade = json.loads(request.content)["trade"]
            posted.append(trade)
            if trade["execution_type"] == "MARKET":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "success": True,
                            "order_uuid": "ord-flat-1",
                            "order": {"orderUuid": "ord-flat-1", "filled": True, "quantity": trade["quantity"]},
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {"success": True}})
        raise AssertionError(f"Unexpected {request.method} {path}")

    transport = httpx.MockTransport(handler)
    client1 = VantaClient(cfg, transport=transport)
    order_client1 = VantaOrderClient(cfg, confirm_account_id=account_id, allow_execution=True, transport=transport)
    with VantaGateway(client1, account_id=account_id, order_client=order_client1) as gw:
        gw.observe(now_ms)
        close_out = gw.submit(
            {"intent_id": "i-close", "market": "BTCUSDC", "quantity": "-0.02", "reduce_only": True}
        )
        assert close_out.status == "FILLED"
        assert posted[-1]["execution_type"] == "MARKET"
        assert posted[-1]["order_type"] == "FLAT"
        assert posted[-1]["quantity"] == 0.02

    client2 = VantaClient(cfg, transport=transport)
    order_client2 = VantaOrderClient(cfg, confirm_account_id=account_id, allow_execution=True, transport=transport)
    with VantaGateway(
        client2,
        account_id=account_id,
        order_client=order_client2,
        broker_id_lookup=lambda iid: "ord-persisted" if iid == "i-limit" else None,
    ) as gw2:
        cancel_out = gw2.cancel("i-limit")
        assert cancel_out == Outcome(status="CANCELLED", broker_id="ord-persisted")
        assert posted[-1]["execution_type"] == "LIMIT_CANCEL"
        assert posted[-1]["order_uuid"] == "ord-persisted"


def test_vanta_gateway_limit_orders_binding_cap_cancel_errors_and_paused_identity(tmp_path: Path) -> None:
    httpx = pytest.importorskip("httpx")
    vanta_mod = pytest.importorskip("connectors.vanta")
    VantaAuthenticationError = vanta_mod.VantaAuthenticationError
    VantaClient = vanta_mod.VantaClient
    VantaConfig = vanta_mod.VantaConfig
    VantaOrderClient = vanta_mod.VantaOrderClient

    account_id = "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f"
    cfg = VantaConfig(account_id=account_id, api_key="vk_test_id", api_secret="vks_test_secret")
    now_ms = 1794683600000
    state: dict[str, Any] = {
        "cancel_mode": "404",
        "key_status": 200,
        "paused_until": None,
        "orders": [],
        "posted": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/api/v1/keys/self":
                if state["key_status"] != 200:
                    return httpx.Response(state["key_status"], json={"error": "unauthorized"})
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "keyId": cfg.api_key,
                            "accountId": account_id,
                            "orderPlacementPausedUntil": state["paused_until"],
                        },
                    },
                )
            if path == "/api/v1/trading/account":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "accountId": account_id,
                            "evaluation": {"isEliminated": False},
                            "account": {"currentBalance": 5000, "currentEquity": 5000, "openPnL": 0, "capitalUsed": 0},
                            "meta": {"generatedAtMs": 1794683599000, "stale": False},
                        },
                    },
                )
            if path == "/api/v1/trading/positions":
                return httpx.Response(200, json={"success": True, "data": []})
            if path == "/api/v1/trading/orders":
                return httpx.Response(200, json={"success": True, "data": state["orders"]})
        elif request.method == "POST" and path == "/api/v1/trading/orders":
            trade = json.loads(request.content)["trade"]
            state["posted"].append(trade)
            if trade["execution_type"] == "LIMIT_CANCEL":
                mode = state["cancel_mode"]
                if mode == "404":
                    return httpx.Response(404, json={"error": "order_not_found"})
                if mode == "403":
                    return httpx.Response(403, json={"error": "forbidden"})
                if mode == "503":
                    return httpx.Response(503, json={"error": "unavailable"})
                return httpx.Response(200, json={"success": True, "data": {"success": True}})
            if trade["execution_type"] == "LIMIT":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "success": True,
                            "order_uuid": "ord-lim-1",
                            "order": {"orderUuid": "ord-lim-1", "filled": False},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "success": True,
                        "order_uuid": "ord-mkt-cap",
                        "order": {"orderUuid": "ord-mkt-cap", "filled": True, "bindingCap": "CRYPTO_LEVERAGE"},
                    },
                },
            )
        raise AssertionError(f"Unexpected {request.method} {path}")

    transport = httpx.MockTransport(handler)
    client = VantaClient(cfg, transport=transport)
    order_client = VantaOrderClient(cfg, confirm_account_id=account_id, allow_execution=True, transport=transport)

    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    _write_cwsa_db(source_db, seq=1, next_cp=now_ms, positions_by_sid={"ACT04": []})
    ex = CwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001")},
        rulebook_key="vanta-trading:classic_1step_5pct:phase_1",
        allow_rest_reconciliation=True,
        reset_time_hhmm="00:00",
    )

    with VantaGateway(
        client,
        account_id=account_id,
        order_client=order_client,
        broker_id_lookup=ex.lookup_broker_id,
    ) as gw:
        lim_out = gw.submit(
            {
                "intent_id": "i-lim-1",
                "market": "BTCUSDC",
                "quantity": "0.01",
                "order_type": "limit",
                "limit_price": "50000",
            }
        )
        assert lim_out == Outcome(status="ACCEPTED", broker_id="ord-lim-1")
        assert state["posted"][-1]["execution_type"] == "LIMIT"
        assert state["posted"][-1]["limit_price"] == 50000

        state["orders"] = [
            {
                "orderUuid": "ord-lim-1",
                "tradePair": "BTCUSDC",
                "orderType": "LONG",
                "executionType": "BRACKET",
                "value": 500,
                "limitPrice": 50000,
            }
        ]
        snap = gw.observe(now_ms)
        assert len(snap.orders) == 1
        assert snap.orders[0].intent_id == "i-lim-1"
        assert snap.orders[0].quantity == Decimal("0.01")
        assert snap.orders[0].reduce_only is True

        cap_out = gw.submit({"intent_id": "i-cap-1", "market": "BTCUSDC", "quantity": "0.02"})
        assert cap_out == Outcome(status="FILLED", broker_id="ord-mkt-cap", reason="BINDING_CAP:CRYPTO_LEVERAGE")

        state["cancel_mode"] = "404"
        assert gw.cancel("i-lim-1") == Outcome(status="UNKNOWN", broker_id="ord-lim-1", reason="ORDER_NOT_FOUND")
        state["cancel_mode"] = "403"
        assert gw.cancel("i-lim-1").status == "REJECTED"
        state["cancel_mode"] = "503"
        assert gw.cancel("i-lim-1") == Outcome(
            status="UNKNOWN", broker_id="ord-lim-1", reason="CANCEL_AMBIGUOUS:VantaServerError"
        )

    # Cached orderPlacementPausedUntil blocks submits without network call
    state["paused_until"] = "2099-01-01T00:00:00.000Z"
    client_paused = VantaClient(cfg, transport=transport)
    order_client_paused = VantaOrderClient(
        cfg, confirm_account_id=account_id, allow_execution=True, transport=transport
    )
    with VantaGateway(client_paused, account_id=account_id, order_client=order_client_paused) as gw_paused:
        gw_paused.observe(now_ms)
        posts_before = len(state["posted"])
        assert gw_paused.submit({"intent_id": "i-paused", "market": "BTCUSDC", "quantity": "0.01"}) == Outcome(
            status="REJECTED", reason="VANTA_COOLDOWN_ACTIVE"
        )
        assert len(state["posted"]) == posts_before

    # Fatal auth errors on observe are never swallowed
    state["key_status"] = 401
    client_auth = VantaClient(cfg, transport=transport)
    with VantaGateway(client_auth, account_id=account_id) as gw_auth:
        with pytest.raises(VantaAuthenticationError):
            gw_auth.observe(now_ms)

    # Executor lookup_broker_id returns persisted broker_id
    ex._record_intent_pending("i-db", "wk-1", "BTCUSDC", Decimal("0.01"), False, now_ms)
    ex._update_intent_outcome("i-db", "ACCEPTED", "ord-from-db", None, now_ms)
    assert ex.lookup_broker_id("i-db") == "ord-from-db"
    assert ex.lookup_broker_id("missing") is None


def test_vanta_gateway_rejection_codes_ack_false_position_tracking_timeout_and_restart_lookup(
    tmp_path: Path,
) -> None:
    httpx = pytest.importorskip("httpx")
    vanta_mod = pytest.importorskip("connectors.vanta")
    VantaClient = vanta_mod.VantaClient
    VantaConfig = vanta_mod.VantaConfig
    VantaOrderClient = vanta_mod.VantaOrderClient

    assert inspect.signature(VantaGateway.__init__).parameters["command_timeout_s"].default == 35.0

    account_id = "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f"
    cfg = VantaConfig(account_id=account_id, api_key="vk_test_id", api_secret="vks_test_secret")
    now_ms = 1794683600000

    state: dict[str, Any] = {
        "post_mode": "422",
        "orders": [],
        "posted": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/api/v1/keys/self":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"keyId": cfg.api_key, "accountId": account_id}},
                )
            if path == "/api/v1/trading/account":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "accountId": account_id,
                            "evaluation": {"isEliminated": False},
                            "account": {
                                "currentBalance": 5000,
                                "currentEquity": 5000,
                                "openPnL": 0,
                                "capitalUsed": 0,
                            },
                            "meta": {"generatedAtMs": 1794683599000, "stale": False},
                        },
                    },
                )
            if path == "/api/v1/trading/positions":
                return httpx.Response(200, json={"success": True, "data": []})
            if path == "/api/v1/trading/orders":
                return httpx.Response(200, json={"success": True, "data": state["orders"]})
        elif request.method == "POST" and path == "/api/v1/trading/orders":
            trade = json.loads(request.content)["trade"]
            state["posted"].append(trade)
            mode = state["post_mode"]
            if mode == "422":
                return httpx.Response(422, json={"error": "unprocessable_entity"})
            if mode == "408":
                return httpx.Response(408, json={"error": "request_timeout"})
            if mode == "ack_false_no_uuid":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"success": False, "error_message": "INSUFFICIENT_MARGIN"},
                    },
                )
            if mode == "cancel_ack_false":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"success": False, "error_message": "ALREADY_CLOSED"},
                    },
                )
            if mode == "fill_full":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "success": True,
                            "order_uuid": f"ord-{len(state['posted'])}",
                            "order": {
                                "orderUuid": f"ord-{len(state['posted'])}",
                                "filled": True,
                                "quantity": trade["quantity"],
                            },
                        },
                    },
                )
            if mode == "fill_partial_001":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "success": True,
                            "order_uuid": f"ord-{len(state['posted'])}",
                            "order": {
                                "orderUuid": f"ord-{len(state['posted'])}",
                                "filled": True,
                                "quantity": 0.01,
                            },
                        },
                    },
                )
            if mode == "fill_no_qty":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "success": True,
                            "order_uuid": f"ord-{len(state['posted'])}",
                            "order": {
                                "orderUuid": f"ord-{len(state['posted'])}",
                                "filled": True,
                            },
                        },
                    },
                )
        raise AssertionError(f"Unexpected {request.method} {path}")

    transport = httpx.MockTransport(handler)
    client = VantaClient(cfg, transport=transport)
    order_client = VantaOrderClient(
        cfg, confirm_account_id=account_id, allow_execution=True, transport=transport
    )

    with VantaGateway(client, account_id=account_id, order_client=order_client) as gw:
        assert gw._loop_runner._timeout_s == 35.0

        state["post_mode"] = "422"
        out_422 = gw.submit({"intent_id": "i-422", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_422 == Outcome(status="REJECTED", broker_id=None, reason="HTTP_422:unprocessable_entity")

        state["post_mode"] = "408"
        out_408 = gw.submit({"intent_id": "i-408", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_408 == Outcome(status="UNKNOWN", broker_id=None, reason="SUBMIT_AMBIGUOUS:VantaAPIError")

        state["post_mode"] = "ack_false_no_uuid"
        out_ack_false = gw.submit({"intent_id": "i-af", "market": "BTCUSDC", "quantity": "0.01"})
        assert out_ack_false == Outcome(
            status="REJECTED", broker_id=None, reason="INSUFFICIENT_MARGIN"
        )

        state["post_mode"] = "fill_full"
        assert (
            gw.submit({"intent_id": "i-open", "market": "BTCUSDC", "quantity": "0.02"}).status
            == "FILLED"
        )
        assert gw._last_positions["BTCUSDC"] == Decimal("0.02")

        state["post_mode"] = "fill_partial_001"
        assert (
            gw.submit(
                {"intent_id": "i-flat-part", "market": "BTCUSDC", "quantity": "-0.02", "reduce_only": True}
            ).status
            == "FILLED"
        )
        assert state["posted"][-1]["order_type"] == "FLAT"
        assert gw._last_positions["BTCUSDC"] == Decimal("0.01")

        state["post_mode"] = "fill_full"
        assert (
            gw.submit(
                {"intent_id": "i-flat-rest", "market": "BTCUSDC", "quantity": "-0.01", "reduce_only": True}
            ).status
            == "FILLED"
        )
        assert state["posted"][-1]["order_type"] == "FLAT"
        assert gw._last_positions["BTCUSDC"] == Decimal("0")

        state["post_mode"] = "fill_no_qty"
        assert (
            gw.submit({"intent_id": "i-short", "market": "ETHUSDC", "quantity": "-0.5"}).status
            == "FILLED"
        )
        assert gw._last_positions["ETHUSDC"] == Decimal("-0.5")

    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    _write_cwsa_db(source_db, seq=1, next_cp=now_ms, positions_by_sid={"ACT04": []})
    ex = CwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001")},
        rulebook_key="vanta-trading:classic_1step_5pct:phase_1",
        allow_rest_reconciliation=True,
        reset_time_hhmm="00:00",
    )
    ex._record_intent_pending("i-lim-restart", "wk-r", "BTCUSDC", Decimal("0.01"), False, now_ms)
    ex._update_intent_outcome("i-lim-restart", "ACCEPTED", "ord-restart-1", None, now_ms)

    state["orders"] = [
        {
            "orderUuid": "ord-restart-1",
            "tradePair": "BTCUSDC",
            "orderType": "LONG",
            "executionType": "LIMIT",
            "quantity": 0.01,
        }
    ]
    state["post_mode"] = "cancel_ack_false"
    client_restarted = VantaClient(cfg, transport=transport)
    order_client_restarted = VantaOrderClient(
        cfg, confirm_account_id=account_id, allow_execution=True, transport=transport
    )
    with VantaGateway(
        client_restarted,
        account_id=account_id,
        order_client=order_client_restarted,
        broker_id_lookup=ex.lookup_broker_id,
        intent_id_lookup=ex.lookup_intent_id,
    ) as gw_restarted:
        snap = gw_restarted.observe(now_ms)
        assert len(snap.orders) == 1
        assert snap.orders[0].intent_id == "i-lim-restart"
        assert snap.orders[0].broker_id == "ord-restart-1"
        assert gw_restarted.cancel("i-lim-restart") == Outcome(
            status="REJECTED", broker_id="ord-restart-1", reason="ALREADY_CLOSED"
        )
