"""Behavioral tests for the 5m CWSA-to-BybitDemoGateway executor."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from prop_production.bybit_executor import (
    TRIADA_HYDRO5K_K,
    BybitCwsaExecutor,
    MarketSpec,
    load_cwsa_targets,
)
from prop_production.gateway import BrokerOrder, BrokerSnapshot, Outcome, Position


def _seed_source_db(path: Path, *, skew: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cwsa_heads (stream TEXT PRIMARY KEY, scope TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL, head TEXT NOT NULL)"
    )
    streams = [
        ("s1", "ACT04", 746, 1794683700000, [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20.0"}]),
        ("s2", "ACT08", 746, 1794683700000 if not skew else 1794684000000, [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-100.0"}]),
        ("s3", "ACT09", 746, 1794683700000, [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10.0"}]),
    ]
    for stream, sid, seq, next_cp, positions in streams:
        conn.execute(
            "INSERT INTO cwsa_heads VALUES (?, ?, ?, ?, ?)",
            (
                stream,
                json.dumps({"source_id": sid}),
                "{}",
                json.dumps(
                    {
                        "sequence": seq,
                        "next_checkpoint_ms": next_cp,
                        "economic_sha": f"econ-{sid}",
                        "positions": positions,
                    }
                ),
                f"head-{sid}",
            ),
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
    equity: Decimal = Decimal("4990"),
    positions: tuple[Position, ...] = (),
    orders: tuple[BrokerOrder, ...] = (),
    financial_complete: bool = True,
    uncertainty_reasons: tuple[str, ...] = (),
) -> BrokerSnapshot:
    return BrokerSnapshot(
        account_binding="binding",
        endpoint="https://api-demo.bybit.com",
        observed_at_ms=1794683500000,
        received_at_ms=1794683500000,
        epoch=1,
        cash=equity,
        equity=equity,
        unrealized=Decimal("0"),
        initial_margin=Decimal("0"),
        maintenance_margin=Decimal("0"),
        positions=positions,
        orders=orders,
        fills=(),
        uncertainty_reasons=uncertainty_reasons,
        financial_complete=financial_complete,
    )


def _markets() -> dict[str, MarketSpec]:
    return {
        "BTC": MarketSpec(symbol="BTC/USDT:USDT", min_amount=Decimal("0.001"), amount_step=Decimal("0.001")),
        "ETH": MarketSpec(symbol="ETH/USDT:USDT", min_amount=Decimal("0.01"), amount_step=Decimal("0.01")),
    }


def test_dry_run_and_execute_idempotency(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    _seed_source_db(source_db)

    executor = BybitCwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
    )
    gw = _FakeGateway(
        _snap(
            positions=(Position("BTC/USDT:USDT", Decimal("0.020"), Decimal("90000"), Decimal("90000")),),
        )
    )

    # BTC target = 20*0.00030 + 10*0.00066 = 0.0126 -> delta from 0.020 = -0.0074 -> quantized -0.007 (reduce_only=True)
    # ETH target = -100*0.00237 = -0.237 -> quantized -0.23 (reduce_only=False)
    dry = executor.step(gw, _markets(), 1794683500000, execute=False)
    assert dry["status"] == "SHADOW_DRY_RUN"
    assert dry["planned_count"] == 2
    assert gw.submitted == []

    by_mkt = {item["market"]: item for item in dry["planned"]}
    assert by_mkt["BTC/USDT:USDT"]["quantity"] == "-0.007"
    assert by_mkt["BTC/USDT:USDT"]["reduce_only"] is True
    assert by_mkt["ETH/USDT:USDT"]["quantity"] == "-0.23"
    assert by_mkt["ETH/USDT:USDT"]["reduce_only"] is False

    live = executor.step(gw, _markets(), 1794683501000, execute=True)
    assert live["status"] == "EXECUTED"
    assert live["submitted"] == 2
    assert len(gw.submitted) == 2

    again = executor.step(gw, _markets(), 1794683502000, execute=True)
    assert again["status"] == "ALREADY_PROCESSED"
    assert len(gw.submitted) == 2


def test_fail_closed_risk_and_skew(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    _seed_source_db(source_db)
    executor = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)

    # Incomplete finance blocks
    gw_inc = _FakeGateway(_snap(financial_complete=False, uncertainty_reasons=("BYBIT_UNIFIED_FINANCE_INCOMPLETE",)))
    res = executor.step(gw_inc, _markets(), 1794683500000, execute=True)
    assert res["status"] == "BLOCKED"
    assert "BYBIT_UNIFIED_FINANCE_INCOMPLETE" in res["reason"]

    # Static DD floor (<= 4500) blocks
    gw_dd = _FakeGateway(_snap(equity=Decimal("4500")))
    res_dd = executor.step(gw_dd, _markets(), 1794683500000, execute=True)
    assert res_dd["status"] == "BLOCKED"
    assert "HALT_STATIC_DD" in res_dd["reason"]

    # Internal daily stop (>= 200 loss in UTC day) blocks
    gw_day1 = _FakeGateway(_snap(equity=Decimal("4990")))
    assert executor.step(gw_day1, _markets(), 1794683500000, execute=False)["status"] == "SHADOW_DRY_RUN"
    gw_day2 = _FakeGateway(_snap(equity=Decimal("4790")))
    res_day = executor.step(gw_day2, _markets(), 1794683505000, execute=True)
    assert res_day["status"] == "BLOCKED"
    assert "HALT_DAILY_STOP" in res_day["reason"]

    # Open order on symbol skips that symbol
    gw_open = _FakeGateway(
        _snap(
            equity=Decimal("4990"),
            orders=(BrokerOrder("intent-1", "oid-1", "BTC/USDT:USDT", Decimal("0.01"), Decimal("0"), "OPEN"),),
        )
    )
    res_open = executor.step(gw_open, _markets(), 1794683510000, execute=False)
    assert any(s.get("symbol") == "BTC/USDT:USDT" and s["reason"] == "OPEN_ORDER_PENDING" for s in res_open["skipped"])

    # Checkpoint skew fails closed
    skew_db = tmp_path / "skew.sqlite3"
    _seed_source_db(skew_db, skew=True)
    with pytest.raises(ValueError, match="CWSA_CHECKPOINT_SKEW"):
        load_cwsa_targets(skew_db, TRIADA_HYDRO5K_K)
