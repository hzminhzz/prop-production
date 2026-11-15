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


def _write_cwsa_db(
    path: Path,
    *,
    seq: int = 746,
    next_cp: int = 1794683700000,
    positions_by_sid: dict[str, list[dict[str, str]]] | None = None,
    phases_by_sid: dict[str, str] | None = None,
    skew: bool = False,
) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cwsa_heads (stream TEXT PRIMARY KEY, scope TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL, head TEXT NOT NULL)"
    )
    default_pos: dict[str, list[dict[str, str]]] = {
        "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20.0"}],
        "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-100.0"}],
        "ACT09": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10.0"}],
    }
    pos_map = positions_by_sid if positions_by_sid is not None else default_pos
    for idx, sid in enumerate(("ACT04", "ACT08", "ACT09"), start=1):
        cp = next_cp + 300_000 if (skew and sid == "ACT08") else next_cp
        pos = pos_map.get(sid, [])
        state: dict[str, Any] = {
            "sequence": seq,
            "next_checkpoint_ms": cp,
            "economic_sha": f"econ-{sid}-{seq}-{len(pos)}",
            "positions": pos,
        }
        if phases_by_sid and sid in phases_by_sid:
            state["phase"] = phases_by_sid[sid]
        conn.execute(
            "INSERT INTO cwsa_heads VALUES (?, ?, ?, ?, ?)",
            (
                f"s{idx}",
                json.dumps({"source_id": sid}),
                "{}",
                json.dumps(state),
                f"head-{sid}",
            ),
        )
    conn.commit()
    conn.close()


def _seed_source_db(path: Path, *, skew: bool = False) -> None:
    _write_cwsa_db(path, skew=skew)


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
        "SOL": MarketSpec(symbol="SOL/USDT:USDT", min_amount=Decimal("0.1"), amount_step=Decimal("0.1")),
        "DOGE": MarketSpec(symbol="DOGE/USDT:USDT", min_amount=Decimal("1"), amount_step=Decimal("1")),
    }


def _seed_empty_baseline(
    executor: BybitCwsaExecutor, source_db: Path, markets: dict[str, MarketSpec], now_ms: int = 1794683400000
) -> None:
    _write_cwsa_db(
        source_db,
        seq=745,
        next_cp=1794683400000,
        positions_by_sid={"ACT04": [], "ACT08": [], "ACT09": []},
    )
    gw_empty = _FakeGateway(_snap())
    res = executor.step(gw_empty, markets, now_ms, execute=True)
    assert res["status"] == "EXECUTED"
    assert res["submitted"] == 0


def test_dry_run_and_execute_idempotency(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()
    executor = BybitCwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
    )
    _seed_empty_baseline(executor, source_db, markets)
    _seed_source_db(source_db)

    gw = _FakeGateway(
        _snap(
            positions=(Position("BTC/USDT:USDT", Decimal("0.020"), Decimal("90000"), Decimal("90000")),),
        )
    )

    # BTC target = 20*0.00045 + 10*0.00099 = 0.0189 -> delta from 0.020 = -0.0011 -> quantized -0.001 (reduce_only=True)
    # ETH target = -100*0.00356 = -0.356 -> quantized -0.35 (reduce_only=False)
    dry = executor.step(gw, markets, 1794683500000, execute=False)
    assert dry["status"] == "SHADOW_DRY_RUN"
    assert dry["planned_count"] == 2
    assert gw.submitted == []

    by_mkt = {item["market"]: item for item in dry["planned"]}
    assert by_mkt["BTC/USDT:USDT"]["quantity"] == "-0.001"
    assert by_mkt["BTC/USDT:USDT"]["reduce_only"] is True
    assert by_mkt["ETH/USDT:USDT"]["quantity"] == "-0.35"
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
    executor = BybitCwsaExecutor(
        state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K, max_source_lag_ms=None
    )

    # Incomplete finance blocks (never auto-flatten on blind snapshot)
    gw_inc = _FakeGateway(
        _snap(
            positions=(Position("BTC/USDT:USDT", Decimal("0.010"), Decimal("90000"), Decimal("90000")),),
            financial_complete=False,
            uncertainty_reasons=("BYBIT_UNIFIED_FINANCE_INCOMPLETE",),
        )
    )
    res = executor.step(gw_inc, _markets(), 1794683500000, execute=True)
    assert res["status"] == "BLOCKED"
    assert gw_inc.submitted == []

    # Internal daily stop (>= 200 loss in UTC day) auto-flattens reduce_only=True, holds same day, resumes next UTC day
    gw_day1 = _FakeGateway(_snap(equity=Decimal("4990")))
    assert executor.step(gw_day1, _markets(), 1794683500000, execute=False)["status"] == "SHADOW_DRY_RUN"
    gw_sweep = _FakeGateway(
        _snap(
            equity=Decimal("4790"),
            positions=(Position("BTC/USDT:USDT", Decimal("0.010"), Decimal("90000"), Decimal("90000")),),
        )
    )
    res_day = executor.step(gw_sweep, _markets(), 1794683505000, execute=True)
    assert res_day["status"] == "HALT_FLATTENED"
    assert "HALT_DAILY_STOP" in res_day["reason"]
    assert gw_sweep.submitted[0]["quantity"] == "-0.010"
    assert gw_sweep.submitted[0]["reduce_only"] is True

    # Same UTC day stays halted even if equity recovers; next UTC day (+86400s) resumes
    res_same_day = executor.step(_FakeGateway(_snap(equity=Decimal("4850"))), _markets(), 1794683510000, execute=False)
    assert res_same_day["status"] == "HALTED"
    res_next_day = executor.step(_FakeGateway(_snap(equity=Decimal("4850"))), _markets(), 1794683500000 + 86_400_000, execute=False)
    assert res_next_day["status"] == "SHADOW_DRY_RUN"

    # Static DD floor (<= 4500) flattens and permanently HALTs across UTC days
    gw_dd = _FakeGateway(
        _snap(
            equity=Decimal("4500"),
            positions=(Position("ETH/USDT:USDT", Decimal("-0.10"), Decimal("3000"), Decimal("3000")),),
        )
    )
    res_dd = executor.step(gw_dd, _markets(), 1794683500000 + 86_400_000, execute=True)
    assert res_dd["status"] == "HALT_FLATTENED"
    assert "HALT_STATIC_DD" in res_dd["reason"]
    assert gw_dd.submitted[0]["quantity"] == "0.10"
    assert gw_dd.submitted[0]["reduce_only"] is True
    res_dd_next = executor.step(_FakeGateway(_snap(equity=Decimal("4900"))), _markets(), 1794683500000 + 172_800_000, execute=False)
    assert res_dd_next["status"] == "HALTED"

    # Open order on symbol skips that symbol (using fresh executor with empty baseline before permanent HARD_HALT)
    source_db2 = tmp_path / "source2.sqlite3"
    executor2 = BybitCwsaExecutor(
        state_db=tmp_path / "exec2.sqlite", source_db=source_db2, member_k=TRIADA_HYDRO5K_K
    )
    _seed_empty_baseline(executor2, source_db2, _markets())
    _seed_source_db(source_db2)
    gw_open = _FakeGateway(
        _snap(
            equity=Decimal("4990"),
            orders=(BrokerOrder("intent-1", "oid-1", "BTC/USDT:USDT", Decimal("0.01"), Decimal("0"), "OPEN"),),
        )
    )
    res_open = executor2.step(gw_open, _markets(), 1794683510000, execute=False)
    assert any(s.get("symbol") == "BTC/USDT:USDT" and s["reason"] == "OPEN_ORDER_PENDING" for s in res_open["skipped"])

    # Checkpoint skew fails closed
    skew_db = tmp_path / "skew.sqlite3"
    _seed_source_db(skew_db, skew=True)
    with pytest.raises(ValueError, match="CWSA_CHECKPOINT_SKEW"):
        load_cwsa_targets(skew_db, TRIADA_HYDRO5K_K)


def test_filled_orders_not_skipped_k_changes_wave_and_stale_blocks(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    ex = BybitCwsaExecutor(
        state_db=tmp_path / "exec.sqlite", source_db=source_db, member_k=TRIADA_HYDRO5K_K
    )
    _seed_empty_baseline(ex, source_db, _markets())
    _seed_source_db(source_db)
    gw_filled = _FakeGateway(
        _snap(orders=(BrokerOrder("i1", "o1", "BTC/USDT:USDT", Decimal("0.01"), Decimal("0.01"), "FILLED"),))
    )
    res = ex.step(gw_filled, _markets(), 1794683500000, execute=False)
    assert res["skipped"] == []
    assert res["planned_count"] == 2

    wk1, _, _ = load_cwsa_targets(source_db, TRIADA_HYDRO5K_K)
    wk2, _, _ = load_cwsa_targets(source_db, {**TRIADA_HYDRO5K_K, "ACT04": Decimal("0.00030")})
    assert wk1 != wk2

    stale = ex.step(gw_filled, _markets(), 1794683700000 + 600_001, execute=False)
    assert stale["status"] == "BLOCKED"
    assert "BLOCKED_CWSA_STALE" in stale["reason"]


def test_fresh_activation_ignores_existing_positions_until_closed_or_flipped(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()
    executor = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)
    gw = _FakeGateway(_snap())

    # Wave 0: pre-existing ACT04 BTC (+20), ACT08 ETH (-100), ACT09 BTC (+10), SOL (+50) -> 0 orders
    _write_cwsa_db(
        source_db,
        seq=746,
        next_cp=1794683700000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"}],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-100"}],
            "ACT09": [
                {"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10"},
                {"instrument": "hyperliquid/mainnet:perp:default/SOL", "quantity": "50"},
            ],
        },
    )
    dry0 = executor.step(gw, markets, 1794683600000, execute=False)
    assert dry0["planned_count"] == 0
    assert dry0["ignored_legs_count"] == 4
    exec0 = executor.step(gw, markets, 1794683601000, execute=True)
    assert exec0["status"] == "EXECUTED"
    assert exec0["submitted"] == 0
    assert exec0["ignored_legs_count"] == 4
    assert gw.submitted == []

    # Wave 1: ACT08 partially closes ETH (-100 -> -60), ACT09 scales up SOL (+50 -> +80), ACT04 opens new DOGE (+10000)
    # Only DOGE should open; ETH partial close must NOT generate an inverted BUY, SOL scale-up stays ignored.
    _write_cwsa_db(
        source_db,
        seq=747,
        next_cp=1794684000000,
        positions_by_sid={
            "ACT04": [
                {"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"},
                {"instrument": "hyperliquid/mainnet:perp:default/DOGE", "quantity": "10000"},
            ],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-60"}],
            "ACT09": [
                {"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10"},
                {"instrument": "hyperliquid/mainnet:perp:default/SOL", "quantity": "80"},
            ],
        },
    )
    res1 = executor.step(gw, markets, 1794683900000, execute=True)
    assert res1["submitted"] == 1
    assert res1["results"][0]["market"] == "DOGE/USDT:USDT"
    assert res1["results"][0]["quantity"] == "4"  # 10000 * 0.00045 = 4.5 -> step 1 -> 4

    # Wave 2: ACT08 fully closes ETH (0) -> un-ignores (ACT08, ETH), 0 orders
    _write_cwsa_db(
        source_db,
        seq=748,
        next_cp=1794684300000,
        positions_by_sid={
            "ACT04": [
                {"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"},
                {"instrument": "hyperliquid/mainnet:perp:default/DOGE", "quantity": "10000"},
            ],
            "ACT08": [],
            "ACT09": [
                {"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10"},
                {"instrument": "hyperliquid/mainnet:perp:default/SOL", "quantity": "80"},
            ],
        },
    )
    gw_with_doge = _FakeGateway(
        _snap(positions=(Position("DOGE/USDT:USDT", Decimal("4"), Decimal("0.2"), Decimal("0.2")),))
    )
    res2 = executor.step(gw_with_doge, markets, 1794684200000, execute=True)
    assert res2["submitted"] == 0
    assert res2["ignored_legs_count"] == 3

    # Wave 3: ACT08 enters fresh ETH (-50) AND ACT09 flips SOL across zero (+80 -> -200) -> BOTH copy!
    _write_cwsa_db(
        source_db,
        seq=749,
        next_cp=1794684600000,
        positions_by_sid={
            "ACT04": [
                {"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"},
                {"instrument": "hyperliquid/mainnet:perp:default/DOGE", "quantity": "10000"},
            ],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-50"}],
            "ACT09": [
                {"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10"},
                {"instrument": "hyperliquid/mainnet:perp:default/SOL", "quantity": "-200"},
            ],
        },
    )
    res3 = executor.step(gw_with_doge, markets, 1794684500000, execute=True)
    assert res3["submitted"] == 2
    assert res3["ignored_legs_count"] == 2
    by_mkt = {r["market"]: r["quantity"] for r in res3["results"]}
    assert by_mkt == {"ETH/USDT:USDT": "-0.17", "SOL/USDT:USDT": "-0.1"}  # -50*0.00356=-0.178; -200*0.00099=-0.198


def test_cross_wallet_netting_with_ignored_leg_and_restart_persistence(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()
    ex1 = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)

    # Activation: ACT08 already holds BTC (-10, ignored); ACT04 flat
    _write_cwsa_db(
        source_db,
        seq=746,
        next_cp=1794683700000,
        positions_by_sid={
            "ACT04": [],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "-10"}],
            "ACT09": [],
        },
    )
    gw = _FakeGateway(_snap())
    assert ex1.step(gw, markets, 1794683600000, execute=True)["submitted"] == 0

    # ACT04 opens fresh BTC (+20) while ACT08 partially closes (-10 -> -5); only ACT04 copies (+0.009)
    _write_cwsa_db(
        source_db,
        seq=747,
        next_cp=1794684000000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"}],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "-5"}],
            "ACT09": [],
        },
    )
    res = ex1.step(gw, markets, 1794683900000, execute=True)
    assert res["submitted"] == 1
    assert res["results"][0]["quantity"] == "0.009"

    # Restart executor instance on same state_db; ACT08 BTC remains ignored on next tick
    ex2 = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)
    _write_cwsa_db(
        source_db,
        seq=748,
        next_cp=1794684300000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"}],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "-3"}],
            "ACT09": [],
        },
    )
    gw_held = _FakeGateway(
        _snap(positions=(Position("BTC/USDT:USDT", Decimal("0.009"), Decimal("90000"), Decimal("90000")),))
    )
    res_restart = ex2.step(gw_held, markets, 1794684200000, execute=True)
    assert res_restart["submitted"] == 0
    assert res_restart["ignored_legs_count"] == 1


def test_migrating_live_db_preserves_open_positions_and_ignores_unheld(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()
    ex = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)

    # Simulate legacy executor.sqlite where the current wave was already processed before upgrade
    _write_cwsa_db(
        source_db,
        seq=746,
        next_cp=1794683700000,
        positions_by_sid={
            "ACT04": [],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-100"}],
            "ACT09": [{"instrument": "hyperliquid/mainnet:perp:default/SOL", "quantity": "50"}],
        },
    )
    from prop_production.bybit_executor import load_cwsa_targets

    cur_wave_key, _, _ = load_cwsa_targets(source_db, TRIADA_HYDRO5K_K)
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "INSERT INTO processed_waves VALUES (?, 1794683700000, 1794683600000, 26)",
            (cur_wave_key,),
        )
        conn.commit()
    finally:
        conn.close()

    # Broker currently holds ETH (-0.35); SOL is flat on broker -> ETH adopted, SOL ignored immediately
    # even on ALREADY_PROCESSED current wave, so a brand-new BTC entry on next wave is NOT swallowed.
    gw = _FakeGateway(
        _snap(positions=(Position("ETH/USDT:USDT", Decimal("-0.35"), Decimal("3000"), Decimal("3000")),))
    )
    already = ex.step(gw, markets, 1794683601000, execute=True)
    assert already["status"] == "ALREADY_PROCESSED"
    _write_cwsa_db(
        source_db,
        seq=747,
        next_cp=1794684000000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20"}],
            "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-100"}],
            "ACT09": [{"instrument": "hyperliquid/mainnet:perp:default/SOL", "quantity": "50"}],
        },
    )
    dry = ex.step(gw, markets, 1794683900000, execute=False)
    assert dry["ignored_legs_count"] == 1  # only pre-existing flat SOL ignored, NOT new BTC
    assert [p["market"] for p in dry["planned"]] == ["BTC/USDT:USDT"]

    # When ACT08 closes ETH -> Hydro exits ETH cleanly (reduce_only=True), while SOL remains ignored
    _write_cwsa_db(
        source_db,
        seq=748,
        next_cp=1794684300000,
        positions_by_sid={
            "ACT04": [],
            "ACT08": [],
            "ACT09": [{"instrument": "hyperliquid/mainnet:perp:default/SOL", "quantity": "50"}],
        },
    )
    exit_res = ex.step(gw, markets, 1794684200000, execute=True)
    assert exit_res["submitted"] == 1
    assert exit_res["ignored_legs_count"] == 1
    assert gw.submitted[0]["market"] == "ETH/USDT:USDT"
    assert gw.submitted[0]["quantity"] == "0.35"
    assert gw.submitted[0]["reduce_only"] is True


def test_non_continuous_cwsa_phase_blocks_entries_but_allows_dd_flatten(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()
    ex = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)
    _write_cwsa_db(source_db, phases_by_sid={"ACT08": "FENCED"})

    blocked = ex.step(_FakeGateway(_snap(equity=Decimal("4990"))), markets, 1794683600000, execute=True)
    assert blocked["status"] == "BLOCKED"
    assert blocked["reason"] == "BLOCKED_CWSA_NOT_CONTINUOUS:ACT08:FENCED"

    # DD flatten still runs even if CWSA is FENCED
    gw_dd = _FakeGateway(
        _snap(
            equity=Decimal("4500"),
            positions=(Position("ETH/USDT:USDT", Decimal("-0.10"), Decimal("3000"), Decimal("3000")),),
        )
    )
    dd = ex.step(gw_dd, markets, 1794683605000, execute=True)
    assert dd["status"] == "HALT_FLATTENED"
    assert gw_dd.submitted[0]["reduce_only"] is True


def test_notional_deadband_filters_micro_tweaks_but_allows_full_close_and_halt(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()
    ex = BybitCwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001"), "ACT08": Decimal("0"), "ACT09": Decimal("0")},
        min_trade_notional_usd=Decimal("30"),
    )
    _seed_empty_baseline(ex, source_db, markets)

    # Fresh opening with no mark price fails open on deadband so new entry opens cleanly (0.005 ETH = $15)
    _write_cwsa_db(
        source_db,
        seq=746,
        next_cp=1794683700000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "10"}],
        },
    )
    gw_open = _FakeGateway(_snap())
    res_open = ex.step(gw_open, markets, 1794683600000, execute=True)
    assert res_open["submitted"] == 1
    assert gw_open.submitted[0]["quantity"] == "0.01"

    # Follow-up tweak: ACT04 adds +10 ETH -> delta +0.01 ETH at $2000 = $20 < $30 -> skipped
    gw_hold = _FakeGateway(
        _snap(positions=(Position("ETH/USDT:USDT", Decimal("0.01"), Decimal("2000"), Decimal("2000")),))
    )
    _write_cwsa_db(
        source_db,
        seq=747,
        next_cp=1794684000000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "20"}],
        },
    )
    res_skip = ex.step(gw_hold, markets, 1794683800000, execute=True)
    assert res_skip["status"] == "EXECUTED"
    assert res_skip["submitted"] == 0
    assert res_skip["skipped"] == [{"symbol": "ETH/USDT:USDT", "reason": "BELOW_NOTIONAL_DEADBAND"}]

    # Next bar adds another +10 ETH -> accumulated drift is now +0.02 ETH * $2000 = $40 >= $30 -> executes
    _write_cwsa_db(
        source_db,
        seq=748,
        next_cp=1794684300000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "30"}],
        },
    )
    res_catchup = ex.step(gw_hold, markets, 1794684100000, execute=True)
    assert res_catchup["submitted"] == 1
    assert gw_hold.submitted[0]["quantity"] == "0.02"

    # Residual full exit (target == 0) bypasses deadband even if remaining position is worth < $30 ($20)
    _write_cwsa_db(
        source_db,
        seq=749,
        next_cp=1794684600000,
        positions_by_sid={"ACT04": []},
    )
    res_close = ex.step(gw_hold, markets, 1794684400000, execute=True)
    assert res_close["submitted"] == 1  # catchup mock still holds 0.01 -> closes 0.01 cleanly
    assert len(gw_hold.submitted) == 2
    assert gw_hold.submitted[-1]["quantity"] == "-0.01"
    assert gw_hold.submitted[-1]["reduce_only"] is True


def test_post_wave_jitter_defers_live_only_and_never_delays_halts(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()

    with pytest.raises(ValueError, match="JITTER_EXCEEDS_MAX_SOURCE_LAG"):
        BybitCwsaExecutor(
            state_db=state_db,
            source_db=source_db,
            member_k=TRIADA_HYDRO5K_K,
            max_source_lag_ms=600_000,
            jitter_min_seconds=0,
            jitter_max_seconds=600,
        )

    ex = BybitCwsaExecutor(
        state_db=state_db,
        source_db=source_db,
        member_k={"ACT04": Decimal("0.001"), "ACT08": Decimal("0"), "ACT09": Decimal("0")},
        jitter_min_seconds=10,
        jitter_max_seconds=45,
        jitter_seed="fixed-test-seed",
    )
    _seed_empty_baseline(ex, source_db, markets)

    _write_cwsa_db(
        source_db,
        seq=746,
        next_cp=1794683700000,
        positions_by_sid={
            "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10"}],
        },
    )
    gw = _FakeGateway(_snap())

    # Dry-run shadow does not wait on jitter
    dry = ex.step(gw, markets, 1794683600000, execute=False)
    assert dry["status"] == "SHADOW_DRY_RUN"
    assert dry["planned_count"] == 1

    # Execute poll at arrival defers cleanly inside [10s, 45s]
    deferred = ex.step(gw, markets, 1794683600000, execute=True)
    assert deferred["status"] == "JITTER_DEFERRED"
    assert deferred["submitted"] == 0
    assert 10_000 <= deferred["remaining_ms"] <= 45_000
    eligible_ms = deferred["eligible_at_ms"]

    # Poll 1ms before eligible_at_ms still defers without changing eligible_at_ms
    still_wait = ex.step(gw, markets, eligible_ms - 1, execute=True)
    assert still_wait["status"] == "JITTER_DEFERRED"
    assert still_wait["eligible_at_ms"] == eligible_ms

    # Poll at eligible_at_ms dispatches cleanly
    fired = ex.step(gw, markets, eligible_ms, execute=True)
    assert fired["status"] == "EXECUTED"
    assert fired["submitted"] == 1

    # Emergency DD halt flatten never defers for jitter
    gw_dd = _FakeGateway(
        _snap(
            equity=Decimal("4500"),
            positions=(Position("BTC/USDT:USDT", Decimal("0.01"), Decimal("90000"), Decimal("90000")),),
        )
    )
    halted = ex.step(gw_dd, markets, eligible_ms + 100, execute=True)
    assert halted["status"] == "HALT_FLATTENED"
    assert Decimal(gw_dd.submitted[0]["quantity"]) == Decimal("-0.01")


def test_cli_global_defaults_and_hydro_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import ModuleType

    from prop_production import bybit_executor

    captured: list[dict[str, Any]] = []

    class _StubRest:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

    class _StubGateway:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _StubGateway:
            return self

        def __exit__(self, *_a: Any) -> None:
            pass

    connectors_pkg = ModuleType("connectors")
    connectors_propr = ModuleType("connectors.propr")
    connectors_propr_rest = ModuleType("connectors.propr.rest")
    connectors_propr_rest.ProprRest = _StubRest  # type: ignore[attr-defined]
    connectors_propr_rest.ProprOrderClient = _StubRest  # type: ignore[attr-defined]
    connectors_pkg.propr = connectors_propr  # type: ignore[attr-defined]
    connectors_propr.rest = connectors_propr_rest  # type: ignore[attr-defined]

    monkeypatch.setenv("PROPR_API_KEY", "k")
    monkeypatch.setitem(sys.modules, "connectors", connectors_pkg)
    monkeypatch.setitem(sys.modules, "connectors.propr", connectors_propr)
    monkeypatch.setitem(sys.modules, "connectors.propr.rest", connectors_propr_rest)
    monkeypatch.setattr("prop_production.propr_gateway.ProprGateway", _StubGateway)

    def _fake_loop(executor: BybitCwsaExecutor, *_a: Any, **_kw: Any) -> int:
        captured.append(
            {
                "min_notional": executor._min_trade_notional,
                "jitter_min_ms": executor._jitter_min_ms,
                "jitter_max_ms": executor._jitter_max_ms,
            }
        )
        return 0

    monkeypatch.setattr(bybit_executor, "_run_rest_poll_loop", _fake_loop)
    markets_file = tmp_path / "markets.json"
    markets_file.write_text('{"BTC": {"symbol": "BTC", "min_amount": "0.001", "amount_step": "0.001"}}')
    state_db = str(tmp_path / "cli_exec.sqlite")

    assert bybit_executor.main(
        [
            "--venue",
            "propr",
            "--propr-account-id",
            "acc-1",
            "--state-db",
            state_db,
            "--markets-json",
            str(markets_file),
            "--once",
        ]
    ) == 0
    assert captured[-1] == {"min_notional": Decimal("30"), "jitter_min_ms": 0, "jitter_max_ms": 45_000}

    assert bybit_executor.main(
        [
            "--venue",
            "propr",
            "--propr-account-id",
            "acc-1",
            "--state-db",
            state_db,
            "--markets-json",
            str(markets_file),
            "--min-trade-notional-usd",
            "15",
            "--jitter-max-seconds",
            "0",
            "--once",
        ]
    ) == 0
    assert captured[-1] == {"min_notional": Decimal("15"), "jitter_min_ms": 0, "jitter_max_ms": 0}
