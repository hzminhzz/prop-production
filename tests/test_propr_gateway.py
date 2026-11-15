"""Tests for rulebook-driven CwsaExecutor risk modes and ProprGateway."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from prop_production.bybit_executor import BybitCwsaExecutor, CwsaExecutor, MarketSpec
from prop_production.gateway import BrokerSnapshot, Outcome, Position
from prop_production.propr_gateway import PROPR_API_URL, ProprGateway, propr_account_binding


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
    default_pos: dict[str, list[dict[str, str]]] = {
        "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20.0"}],
    }
    pos_map = positions_by_sid if positions_by_sid is not None else default_pos
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


class _FakeGateway:
    def __init__(self, snap: BrokerSnapshot) -> None:
        self._snap = snap
        self.submitted: list[dict[str, Any]] = []

    def observe(self, _now_ms: int) -> BrokerSnapshot:
        return self._snap

    def submit(self, intent: dict[str, Any]) -> Outcome:
        self.submitted.append(intent)
        return Outcome(status="FILLED", broker_id=f"oid-{len(self.submitted)}")


def _snap(
    *,
    equity: Decimal,
    balance: Decimal | None = None,
    cash: Decimal | None = None,
    positions: tuple[Position, ...] = (),
    financial_complete: bool = True,
    uncertainty_reasons: tuple[str, ...] = (),
    inventory_complete: bool = True,
) -> BrokerSnapshot:
    return BrokerSnapshot(
        account_binding="binding",
        endpoint="https://example.invalid",
        observed_at_ms=1794683500000,
        received_at_ms=1794683500000,
        epoch=1,
        cash=cash if cash is not None else equity,
        equity=equity,
        unrealized=Decimal("0"),
        initial_margin=Decimal("0"),
        maintenance_margin=Decimal("0"),
        positions=positions,
        orders=(),
        fills=(),
        financial_complete=financial_complete,
        provider_balance=balance,
        evidence_schema="propr_native_evidence_v1",
        native_inventory_read_complete=inventory_complete,
        uncertainty_reasons=uncertainty_reasons,
    )


def _propr_markets() -> dict[str, MarketSpec]:
    return {
        "BTC": MarketSpec(symbol="BTC", min_amount=Decimal("0.001"), amount_step=Decimal("0.001")),
        "ETH": MarketSpec(symbol="ETH", min_amount=Decimal("0.01"), amount_step=Decimal("0.01")),
        "HYPE": MarketSpec(symbol="HYPE", min_amount=Decimal("0.01"), amount_step=Decimal("0.01")),
    }


def test_cwsa_executor_supports_intraday_trailing_lock_at_initial(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    _write_cwsa_db(source_db)
    ex = CwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001")},
        rulebook_key="propr:classic_2step:phase_1",  # initial=5000, trailing=400, lock_at_initial=True
        internal_daily_stop_usd=Decimal("500"),
    )
    markets = _propr_markets()
    day0 = 1794683600000  # 2026-11-14

    # Seed empty initial baseline wave so subsequent entries execute
    _write_cwsa_db(source_db, seq=1, next_cp=day0 - 1000, positions_by_sid={"ACT04": []})
    ex.step(_FakeGateway(_snap(equity=Decimal("5300"), balance=Decimal("5300"))), markets, day0 - 1000, execute=True)

    # Peak reaches 5600 -> uncapped trailing floor would be 5200, locked at initial=5000
    _write_cwsa_db(source_db, seq=2, next_cp=day0)
    res1 = ex.step(_FakeGateway(_snap(equity=Decimal("5600"), balance=Decimal("5600"))), markets, day0, execute=True)
    assert res1["status"] == "EXECUTED"

    # Equity pulls back to 5150 (daily loss 5300-5150=150 < 500) -> safe above locked floor 5000
    _write_cwsa_db(source_db, seq=3, next_cp=day0 + 1000)
    res2 = ex.step(_FakeGateway(_snap(equity=Decimal("5150"), balance=Decimal("5600"))), markets, day0 + 1000, execute=False)
    assert res2["status"] == "SHADOW_DRY_RUN"

    # Equity drops to 4995 <= 5000 -> HALT_TRAILING_DD flattens
    gw_halt = _FakeGateway(
        _snap(
            equity=Decimal("4995"),
            balance=Decimal("5600"),
            positions=(Position("BTC", Decimal("0.01"), Decimal("90000"), Decimal("89000")),),
        )
    )
    res3 = ex.step(gw_halt, markets, day0 + 2000, execute=True)
    assert res3["status"] == "HALT_FLATTENED"
    assert res3["reason"].startswith("HALT_TRAILING_DD:")
    assert gw_halt.submitted[0]["reduce_only"] is True


def test_cwsa_executor_supports_eod_trailing_rollover(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    day1 = 1794683600000
    day2 = day1 + 86_400_000
    _write_cwsa_db(source_db, seq=1, next_cp=day1)
    ex = BybitCwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001")},
        rulebook_key="vanta-trading:grow:funded",  # initial=5000, trailing=400 EOD_EQUITY
        internal_daily_stop_usd=Decimal("500"),
        max_source_lag_ms=None,
    )
    markets = _propr_markets()

    # Intraday spike to 5600 then finishes day1 at 5200 -> EOD peak locks 5200 (floor=4800), NOT 5600
    ex.step(_FakeGateway(_snap(equity=Decimal("5600"))), markets, day1, execute=False)
    ex.step(_FakeGateway(_snap(equity=Decimal("5200"))), markets, day1 + 1000, execute=False)

    # On day2 equity at 4850: above EOD floor 4800, would fail if intraday HWM 5200 applied
    _write_cwsa_db(source_db, seq=2, next_cp=day2)
    res_day2_ok = ex.step(_FakeGateway(_snap(equity=Decimal("4850"))), markets, day2, execute=False)
    assert res_day2_ok["status"] == "SHADOW_DRY_RUN"

    # Drop to 4790 <= 4800 -> halts with HALT_TRAILING_DD reason
    res_day2_halt = ex.step(
        _FakeGateway(_snap(equity=Decimal("4790"), positions=(Position("BTC", Decimal("0.01"), Decimal("90000"), Decimal("89000")),))),
        markets,
        day2 + 1000,
        execute=False,
    )
    assert res_day2_halt["status"] == "SHADOW_HALT_FLATTEN"
    assert res_day2_halt["reason"].startswith("HALT_TRAILING_DD:")


def test_cwsa_executor_rest_mode_allows_propr_observability_and_blocks_real_faults(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    _write_cwsa_db(source_db, seq=1, positions_by_sid={"ACT04": []})
    ex = CwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001")},
        rulebook_key="propr:classic_1step:phase_1",  # daily_basis=BALANCE, daily_loss=150 (stop=120)
        allow_rest_reconciliation=True,
    )
    markets = _propr_markets()
    propr_reasons = (
        "PROVIDER_CASH_UNVERIFIED",
        "PROVIDER_CUTOFF_UNPROVEN",
        "RECONCILIATION_UNCERTAIN",
        "STREAM_NOT_CONNECTED",
    )

    # Allowed REST observability + BALANCE basis uses provider_balance when cash is None
    snap_ok = BrokerSnapshot(
        account_binding="b",
        endpoint=PROPR_API_URL,
        observed_at_ms=1794683500000,
        received_at_ms=1794683500000,
        epoch=0,
        cash=None,
        equity=Decimal("5000"),
        unrealized=Decimal("0"),
        initial_margin=Decimal("0"),
        maintenance_margin=Decimal("0"),
        financial_complete=False,
        provider_balance=Decimal("5000"),
        evidence_schema="propr_native_evidence_v1",
        native_inventory_read_complete=True,
        uncertainty_reasons=propr_reasons,
    )
    ex.step(_FakeGateway(snap_ok), markets, 1794683600000, execute=True)

    # New BTC entry wave executes on Propr without HYRO_POLICY_BLOCKED
    _write_cwsa_db(source_db, seq=2, next_cp=1794684000000)
    res = ex.step(_FakeGateway(snap_ok), markets, 1794683900000, execute=True)
    assert res["status"] == "EXECUTED"
    assert res["submitted"] == 1  # BTC multi-asset allowed (not HYRO_POLICY_BLOCKED)

    # Real fault (stale timestamp) still blocks immediately
    snap_stale = BrokerSnapshot(
        account_binding="b",
        endpoint=PROPR_API_URL,
        observed_at_ms=1794683500000,
        received_at_ms=1794683500000,
        epoch=0,
        cash=None,
        equity=Decimal("5000"),
        unrealized=Decimal("0"),
        initial_margin=Decimal("0"),
        maintenance_margin=Decimal("0"),
        financial_complete=False,
        provider_balance=Decimal("5000"),
        evidence_schema="propr_native_evidence_v1",
        native_inventory_read_complete=True,
        uncertainty_reasons=propr_reasons + ("PROVIDER_TIME_STALE",),
    )
    blocked = ex.step(_FakeGateway(snap_stale), markets, 1794683605000, execute=True)
    assert blocked["status"] == "BLOCKED"
    assert "PROVIDER_TIME_STALE" in blocked["reason"]


def test_propr_gateway_end_to_end_with_mock_transport() -> None:
    ProprRest = pytest.importorskip("connectors.propr.rest").ProprRest
    ProprOrderClient = pytest.importorskip("connectors.propr.rest").ProprOrderClient

    account_id = "urn:prp-account:test-acc"
    now_ms = 1794683600000  # matching 2026-11-14T... UTC below
    seen_orders: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/book-account-issuances"):
                return httpx.Response(200, json={"data": []})
            if path.endswith("/challenge-attempts"):
                return httpx.Response(200, json={"data": [{"accountId": account_id, "attemptId": "att-1"}]})
            if path.endswith(f"/accounts/{account_id}"):
                from datetime import datetime, timezone

                ts = datetime.fromtimestamp((now_ms - 500) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                return httpx.Response(
                    200,
                    json={
                        "accountId": account_id,
                        "updatedAt": ts,
                        "balance": "5000",
                        "availableBalance": "4500",
                        "totalUnrealizedPnl": "0",
                        "isolatedPositionMargin": "0",
                        "totalInitialMargin": "0",
                        "totalMaintenanceMargin": "0",
                    },
                )
            if path.endswith(("/orders", "/positions", "/trades")):
                return httpx.Response(200, json={"data": []})
        elif request.method == "POST":
            body = json.loads(request.content) if request.content else {}
            if path.endswith("/orders"):
                order = body["orders"][0]
                seen_orders.append(order)
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "orderId": f"ord-{len(seen_orders)}",
                                "intentId": order["intentId"],
                                "accountId": account_id,
                                "asset": order["asset"],
                                "status": "filled",
                                "cumulativeQuantity": order["quantity"],
                            }
                        ]
                    },
                )
            if path.endswith("/cancel"):
                return httpx.Response(200, json={"data": {"status": "cancelled"}})
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    rest = ProprRest(PROPR_API_URL, "pk_live_dummy", transport=httpx.MockTransport(handler))
    order_client = ProprOrderClient(
        PROPR_API_URL,
        "pk_live_dummy",
        account_id=account_id,
        confirm_account_id=account_id,
        allow_execution=True,
        transport=httpx.MockTransport(handler),
    )

    with ProprGateway(rest, account_id=account_id, order_client=order_client) as gw:
        snap = gw.observe(now_ms)
        assert snap.account_binding == propr_account_binding(account_id)
        assert snap.provider_balance == Decimal("5000")
        assert snap.cash is None

        # Open long, reduce long, open short, reduce short -> check exact side & positionSide
        cases = [
            ("i-open-long", Decimal("0.5"), False, "buy", "long"),
            ("i-close-long", Decimal("-0.5"), True, "sell", "long"),
            ("i-open-short", Decimal("-1.0"), False, "sell", "short"),
            ("i-close-short", Decimal("1.0"), True, "buy", "short"),
        ]
        for iid, qty, ro, exp_side, exp_pside in cases:
            out = gw.submit({"intent_id": iid, "market": "HYPE", "quantity": str(qty), "reduce_only": ro})
            assert out.status == "FILLED"
            assert seen_orders[-1]["side"] == exp_side
            assert seen_orders[-1]["positionSide"] == exp_pside
            assert seen_orders[-1]["reduceOnly"] is ro

        cancel_out = gw.cancel("i-open-long")
        assert cancel_out.status == "CANCELLED"
        assert cancel_out.broker_id == "ord-1"
