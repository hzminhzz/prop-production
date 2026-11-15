"""Tests for VantaGateway and Vanta rulebook-driven CWSA execution."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from prop_production.bybit_executor import CwsaExecutor
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
