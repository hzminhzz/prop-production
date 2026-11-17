"""Focused regression tests for `prop_production.bybit_executor`."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from prop_production import bybit_executor, prop_firms
from prop_production.bybit_executor import (
    TRIADA_HYDRO5K_K,
    BybitCwsaExecutor,
    load_cwsa_targets,
)
from prop_production.gateway import BrokerSnapshot, MarketSpec, Outcome, Position

_BASE_MS = 1794683500000


def _write_head(
    path: Path,
    *,
    seq: int = 746,
    next_cp: int = 1794683700000,
    positions_by_sid: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    if path.exists():
        path.unlink()
    pos_map = positions_by_sid or {
        "ACT04": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "20.0"}],
        "ACT08": [{"instrument": "hyperliquid/mainnet:perp:default/ETH", "quantity": "-100.0"}],
        "ACT09": [{"instrument": "hyperliquid/mainnet:perp:default/BTC", "quantity": "10.0"}],
    }
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE cwsa_heads "
            "(stream TEXT PRIMARY KEY, scope TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL, head TEXT NOT NULL)"
        )
        for idx, sid in enumerate(("ACT04", "ACT08", "ACT09"), start=1):
            pos = pos_map.get(sid, [])
            state = {
                "sequence": seq,
                "next_checkpoint_ms": next_cp,
                "economic_sha": f"econ-{sid}-{seq}-{len(pos)}",
                "positions": pos,
            }
            conn.execute(
                "INSERT INTO cwsa_heads VALUES (?, ?, ?, ?, ?)",
                (f"s{idx}", json.dumps({"source_id": sid}), "{}", json.dumps(state), f"head-{sid}"),
            )
        conn.commit()


def _snap(
    *,
    equity: Decimal = Decimal("4990"),
    cash: Decimal | None = None,
    positions: tuple[Position, ...] = (),
) -> BrokerSnapshot:
    return BrokerSnapshot(
        account_binding="binding",
        endpoint="https://api-demo.bybit.com",
        observed_at_ms=_BASE_MS,
        received_at_ms=_BASE_MS,
        epoch=1,
        cash=equity if cash is None else cash,
        equity=equity,
        unrealized=Decimal("0"),
        initial_margin=Decimal("0"),
        maintenance_margin=Decimal("0"),
        positions=positions,
        financial_complete=True,
    )


def _markets() -> dict[str, MarketSpec]:
    return {
        "BTC": MarketSpec(symbol="BTC/USDT:USDT", min_amount=Decimal("0.001"), amount_step=Decimal("0.001")),
        "ETH": MarketSpec(symbol="ETH/USDT:USDT", min_amount=Decimal("0.01"), amount_step=Decimal("0.01")),
    }


class _FakeGateway:
    def __init__(self, snap: BrokerSnapshot, outcomes: list[Outcome | Exception] | None = None) -> None:
        self._snap = snap
        self._outcomes = list(outcomes or [])
        self.submitted: list[dict[str, Any]] = []

    def observe(self, _now_ms: int) -> BrokerSnapshot:
        return self._snap

    def submit(self, intent: dict[str, Any]) -> Outcome:
        self.submitted.append(intent)
        if self._outcomes:
            item = self._outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return Outcome(status="FILLED", broker_id=f"oid-{len(self.submitted)}")


def _seed_empty_baseline(ex: BybitCwsaExecutor, source_db: Path, markets: dict[str, MarketSpec]) -> None:
    _write_head(source_db, seq=745, next_cp=_BASE_MS - 100_000, positions_by_sid={"ACT04": [], "ACT08": [], "ACT09": []})
    assert ex.step(_FakeGateway(_snap()), markets, _BASE_MS - 100_000, execute=True)["status"] == "EXECUTED"


def test_submit_exception_and_unknown_outcome_halt_without_marking_wave_processed(tmp_path: Path) -> None:
    markets = _markets()
    for mode, outcome_or_exc in (
        ("exception", RuntimeError("boom")),
        ("unknown", Outcome(status="UNKNOWN", broker_id=None, reason="SUBMIT_AMBIGUOUS:TimeoutError")),
    ):
        sub = tmp_path / mode
        sub.mkdir()
        source_db = sub / "source.sqlite3"
        state_db = sub / "exec.sqlite"
        ex = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)
        _seed_empty_baseline(ex, source_db, markets)
        _write_head(source_db)
        expected_wave_key, _, _ = load_cwsa_targets(source_db, TRIADA_HYDRO5K_K)

        res = ex.step(_FakeGateway(_snap(), [outcome_or_exc]), markets, _BASE_MS, execute=True)
        assert res["status"] == "HALTED"
        assert "HALT_ORDER_UNKNOWN" in res["reason"]
        assert res["submitted"] == 1
        assert res["results"][0]["status"] == "UNKNOWN"
        with closing(sqlite3.connect(state_db)) as conn:
            pw = conn.execute("SELECT 1 FROM processed_waves WHERE wave_key = ?", (expected_wave_key,)).fetchone()
            st = conn.execute("SELECT status FROM order_intents").fetchall()
        assert pw is None
        assert st == [("UNKNOWN",)]

        follow = ex.step(_FakeGateway(_snap()), markets, _BASE_MS + 5_000, execute=True)
        assert follow["status"] == "HALTED"
        assert follow["submitted"] == 0


def test_unresolved_pending_submit_intent_blocks_new_waves_and_retry_resets_rejected_pending(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    markets = _markets()
    ex = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)
    _seed_empty_baseline(ex, source_db, markets)
    _write_head(source_db)

    with closing(sqlite3.connect(state_db)) as conn:
        conn.execute(
            "INSERT INTO order_intents VALUES ('stuck-1', 'wk-0', 'BTC/USDT:USDT', '0.01', 0, "
            "'PENDING_SUBMIT', NULL, NULL, 1794683400000, 1794683400000)"
        )
        conn.execute(
            "INSERT INTO order_intents VALUES ('rej-1', 'wk-1', 'BTC/USDT:USDT', '0.01', 0, "
            "'REJECTED', NULL, 'RATE_LIMIT', 1794683400000, 1794683400000)"
        )
        conn.commit()

    gw = _FakeGateway(_snap())
    blocked = ex.step(gw, markets, _BASE_MS, execute=True)
    assert blocked == {
        "status": "BLOCKED",
        "reason": "BLOCKED_UNRESOLVED_INTENT:stuck-1:BTC/USDT:USDT:PENDING_SUBMIT",
        "submitted": 0,
    }
    assert gw.submitted == []

    ex._record_intent_pending("rej-1", "wk-1", "BTC/USDT:USDT", Decimal("0.01"), False, _BASE_MS + 1000)
    with closing(sqlite3.connect(state_db)) as conn:
        status = conn.execute("SELECT status FROM order_intents WHERE intent_id = 'rej-1'").fetchone()[0]
    assert status == "PENDING_SUBMIT"


def test_halt_flatten_wave_key_and_intent_id_are_idempotent_across_ticks(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    state_db = tmp_path / "exec.sqlite"
    _write_head(source_db)
    ex = BybitCwsaExecutor(state_db=state_db, source_db=source_db, member_k=TRIADA_HYDRO5K_K)
    gw = _FakeGateway(
        _snap(
            equity=Decimal("4500"),
            positions=(Position("BTC/USDT:USDT", Decimal("0.010"), Decimal("90000"), Decimal("90000")),),
        )
    )
    r1 = ex.step(gw, _markets(), _BASE_MS, execute=False)
    r2 = ex.step(gw, _markets(), _BASE_MS + 15_000, execute=False)
    assert r1["status"] == r2["status"] == "SHADOW_HALT_FLATTEN"
    assert r1["wave_key"] == r2["wave_key"]
    assert r1["planned"][0]["intent_id"] == r2["planned"][0]["intent_id"]

    # Execute twice against lagging snapshot: tick 2 must not resubmit or clobber FILLED status.
    e1 = ex.step(gw, _markets(), _BASE_MS + 20_000, execute=True)
    e2 = ex.step(gw, _markets(), _BASE_MS + 35_000, execute=True)
    assert e1["status"] == "HALT_FLATTENED"
    assert e1["submitted"] == 1
    assert e2["submitted"] == 0
    assert e2["skipped"] == [{"symbol": "BTC/USDT:USDT", "reason": "INTENT_ALREADY_SUBMITTED"}]
    assert len(gw.submitted) == 1
    with closing(sqlite3.connect(state_db)) as conn:
        assert conn.execute("SELECT status FROM order_intents").fetchall() == [("FILLED",)]


def test_max_balance_equity_reset_time_unknown_and_day_start_equity_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite3"
    _seed_empty_baseline(
        BybitCwsaExecutor(state_db=tmp_path / "seed.sqlite", source_db=source_db, member_k=TRIADA_HYDRO5K_K),
        source_db,
        _markets(),
    )
    _write_head(source_db)

    ex_unknown = BybitCwsaExecutor(
        state_db=tmp_path / "vanta_unknown.sqlite",
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        rulebook_key="vanta-trading:classic_1step_3pct:phase_1",
    )
    assert ex_unknown.step(_FakeGateway(_snap()), _markets(), _BASE_MS, execute=False) == {
        "status": "BLOCKED",
        "reason": "BLOCKED_RESET_TIME_UNKNOWN",
        "submitted": 0,
    }

    ex_known = BybitCwsaExecutor(
        state_db=tmp_path / "vanta_known.sqlite",
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        rulebook_key="vanta-trading:classic_1step_3pct:phase_1",
        reset_time_hhmm="00:00",
    )
    assert ex_known.step(_FakeGateway(_snap()), _markets(), _BASE_MS, execute=False)["status"] == "SHADOW_DRY_RUN"

    max_be_spec = replace(
        prop_firms.get_challenge_spec("hyrotrader:two_step:phase_1"),
        key="test:max_be:phase_1",
        daily_basis="MAX_BALANCE_EQUITY",
    )
    monkeypatch.setitem(prop_firms.CHALLENGE_RULEBOOKS, max_be_spec.key, max_be_spec)
    ex_mbe = BybitCwsaExecutor(
        state_db=tmp_path / "max_be.sqlite",
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        rulebook_key=max_be_spec.key,
    )
    assert ex_mbe.step(
        _FakeGateway(_snap(cash=Decimal("5000"), equity=Decimal("4850"))), _markets(), _BASE_MS, execute=False
    )["status"] == "SHADOW_DRY_RUN"
    halted_mbe = ex_mbe.step(_FakeGateway(_snap(equity=Decimal("4790"))), _markets(), _BASE_MS + 5_000, execute=False)
    assert halted_mbe["status"] == "HALTED"
    assert "HALT_DAILY_STOP" in halted_mbe["reason"]

    override_db = tmp_path / "override.sqlite"
    ex_override = BybitCwsaExecutor(
        state_db=override_db,
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        max_source_lag_ms=None,
        day_start_equity_usd=Decimal("5100"),
    )
    res_ovr = ex_override.step(_FakeGateway(_snap(equity=Decimal("4890"))), _markets(), _BASE_MS, execute=False)
    assert res_ovr["status"] == "HALTED"
    assert "HALT_DAILY_STOP" in res_ovr["reason"]

    # Restart on next UTC day with the same CLI override must not overwrite day 2's observed baseline.
    ex_restarted = BybitCwsaExecutor(
        state_db=override_db,
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        max_source_lag_ms=None,
        day_start_equity_usd=Decimal("5100"),
    )
    res_next = ex_restarted.step(
        _FakeGateway(_snap(equity=Decimal("4890"))), _markets(), _BASE_MS + 86_400_000, execute=False
    )
    assert res_next["status"] == "SHADOW_DRY_RUN"

    # Running without --day-start-equity clears the sentinel so the same value can apply on a later day.
    ex_plain = BybitCwsaExecutor(
        state_db=override_db,
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        max_source_lag_ms=None,
    )
    assert (
        ex_plain.step(
            _FakeGateway(_snap(equity=Decimal("4890"))), _markets(), _BASE_MS + 86_400_000 + 5_000, execute=False
        )["status"]
        == "SHADOW_DRY_RUN"
    )
    ex_reapplied = BybitCwsaExecutor(
        state_db=override_db,
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        max_source_lag_ms=None,
        day_start_equity_usd=Decimal("5100"),
    )
    res_day3 = ex_reapplied.step(
        _FakeGateway(_snap(equity=Decimal("4890"))), _markets(), _BASE_MS + 2 * 86_400_000, execute=False
    )
    assert res_day3["status"] == "HALTED"
    assert "HALT_DAILY_STOP" in res_day3["reason"]


def test_cli_k_json_reset_time_and_day_start_equity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

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
    connectors_propr_rest.ProprRest = _StubGateway  # type: ignore[attr-defined]
    connectors_propr_rest.ProprOrderClient = _StubGateway  # type: ignore[attr-defined]
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
                "member_k": dict(executor._member_k),
                "reset_time_hhmm": executor._reset_time_hhmm,
                "day_start_equity": executor._day_start_equity_override,
            }
        )
        return 0

    monkeypatch.setattr(bybit_executor, "_run_rest_poll_loop", _fake_loop)
    markets_file = tmp_path / "markets.json"
    markets_file.write_text('{"BTC": {"symbol": "BTC", "min_amount": "0.001", "amount_step": "0.001"}}')
    k_file = tmp_path / "k.json"
    k_file.write_text('{"WALLET_A": "0.002", "WALLET_B": "0.005"}')

    assert (
        bybit_executor.main(
            [
                "--venue",
                "propr",
                "--propr-account-id",
                "acc-1",
                "--state-db",
                str(tmp_path / "cli_k_file.sqlite"),
                "--markets-json",
                str(markets_file),
                "--k-json",
                str(k_file),
                "--reset-time-hhmm",
                "00:05",
                "--day-start-equity",
                "5123.45",
                "--once",
            ]
        )
        == 0
    )
    assert captured[-1] == {
        "member_k": {"WALLET_A": Decimal("0.002"), "WALLET_B": Decimal("0.005")},
        "reset_time_hhmm": "00:05",
        "day_start_equity": Decimal("5123.45"),
    }

    assert (
        bybit_executor.main(
            [
                "--venue",
                "propr",
                "--propr-account-id",
                "acc-1",
                "--state-db",
                str(tmp_path / "cli_k_inline.sqlite"),
                "--markets-json",
                str(markets_file),
                "--k-json",
                '{"WALLET_C": "0.01"}',
                "--once",
            ]
        )
        == 0
    )
    assert captured[-1]["member_k"] == {"WALLET_C": Decimal("0.01")}


def test_rulebook_scale_scales_vanta_10k_floors_and_cli_accepts_vanta_key_id_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite3"
    _write_head(source_db, seq=1, next_cp=_BASE_MS, positions_by_sid={"ACT04": [], "ACT08": [], "ACT09": []})

    ex_daily = BybitCwsaExecutor(
        state_db=tmp_path / "vanta_10k_daily.sqlite",
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        rulebook_key="vanta-trading:classic_1step_3pct:phase_1",
        rulebook_scale=Decimal("2"),
        reset_time_hhmm="00:00",
    )
    assert ex_daily.step(_FakeGateway(_snap(equity=Decimal("10000"))), _markets(), _BASE_MS, execute=False)[
        "status"
    ] == "SHADOW_DRY_RUN"
    res_daily = ex_daily.step(_FakeGateway(_snap(equity=Decimal("9760"))), _markets(), _BASE_MS + 5_000, execute=False)
    assert res_daily["status"] == "HALTED"
    assert "HALT_DAILY_STOP" in res_daily["reason"]

    ex_static = BybitCwsaExecutor(
        state_db=tmp_path / "vanta_10k_static.sqlite",
        source_db=source_db,
        member_k=TRIADA_HYDRO5K_K,
        rulebook_key="vanta-trading:classic_1step_3pct:phase_1",
        rulebook_scale=Decimal("2"),
        reset_time_hhmm="00:00",
    )
    res_static = ex_static.step(_FakeGateway(_snap(equity=Decimal("9500"))), _markets(), _BASE_MS, execute=False)
    assert res_static["status"] == "HALTED"
    assert "HALT_STATIC_DD" in res_static["reason"]

    captured_cfg: list[dict[str, str]] = []

    class _StubConfig:
        def __init__(self, account_id: str, api_key: str, api_secret: str) -> None:
            self.account_id = account_id
            self.api_key = api_key
            self.api_secret = api_secret
            captured_cfg.append({"account_id": account_id, "api_key": api_key, "api_secret": api_secret})

    class _StubClient:
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
    connectors_vanta = ModuleType("connectors.vanta")
    connectors_vanta.VantaConfig = _StubConfig  # type: ignore[attr-defined]
    connectors_vanta.VantaClient = _StubClient  # type: ignore[attr-defined]
    connectors_vanta.VantaOrderClient = _StubClient  # type: ignore[attr-defined]
    connectors_pkg.vanta = connectors_vanta  # type: ignore[attr-defined]

    monkeypatch.delenv("VANTA_API_KEY", raising=False)
    monkeypatch.delenv("VANTA_API_SECRET", raising=False)
    monkeypatch.setenv("VANTA_ACCOUNT_ID", "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f")
    monkeypatch.setenv("VANTA_KEY_ID", "vk_test_id")
    monkeypatch.setenv("VANTA_SECRET", "vks_test_secret")
    monkeypatch.setitem(sys.modules, "connectors", connectors_pkg)
    monkeypatch.setitem(sys.modules, "connectors.vanta", connectors_vanta)
    monkeypatch.setattr("prop_production.vanta_gateway.VantaGateway", _StubGateway)
    monkeypatch.setattr(bybit_executor, "_run_rest_poll_loop", lambda *_a, **_kw: 0)

    markets_file = tmp_path / "markets.json"
    markets_file.write_text('{"BTC": {"symbol": "BTCUSDC", "min_amount": "1e-8", "amount_step": "0"}}')
    assert (
        bybit_executor.main(
            [
                "--venue",
                "vanta",
                "--source-db",
                str(source_db),
                "--state-db",
                str(tmp_path / "cli_vanta.sqlite"),
                "--markets-json",
                str(markets_file),
                "--reset-time-hhmm",
                "00:00",
                "--once",
            ]
        )
        == 0
    )
    assert captured_cfg == [
        {
            "account_id": "6f1c2e34-9a4b-4c1d-8e2f-1a2b3c4d5e6f",
            "api_key": "vk_test_id",
            "api_secret": "vks_test_secret",
        }
    ]
