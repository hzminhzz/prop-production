"""Tests for canonical prop-firm challenge Rulebook catalog, STATIC/BALANCE math, and CLI."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from prop_production.cli import main
from prop_production.finance import DAY_MS, Rulebook, account_step
from prop_production.gateway import fixture_snapshot
from prop_production.prop_firms import (
    CHALLENGE_RULEBOOKS,
    cft_policy,
    challenge_policy,
    consistency_progress,
    get_challenge_spec,
    hyrotrader_policy,
)
from prop_production.risk import fixture_markets, fixture_policy, size_book


def test_all_challenge_rulebooks_validate_and_step() -> None:
    keys = Rulebook.list_challenges()
    assert len(keys) == len(CHALLENGE_RULEBOOKS) == 36
    firms = {get_challenge_spec(k).firm_slug for k in keys}
    assert firms == {
        "northbook",
        "vanta-trading",
        "hyrotrader",
        "crypto-fund-trader",
        "hypernova",
        "propr",
    }

    for key in keys:
        rules = Rulebook.from_challenge(key)
        rules.validate(1000)
        snap = replace(
            fixture_snapshot(1000),
            cash=Decimal(rules.initial),
            equity=Decimal(rules.initial),
            unrealized=Decimal("0"),
        )
        state, reasons = account_step({}, snap, rules, 1000)
        assert not reasons, f"{key} failed initial step: {reasons}"
        assert state["rulebook_sha"] == rules.sha


def test_static_trailing_drawdown_never_trails_upward() -> None:
    rules = Rulebook.from_challenge("hyrotrader:two_step:phase_1")
    assert rules.initial == "5000"
    assert rules.daily_loss == "250"
    assert rules.trailing_loss == "500"
    assert rules.daily_basis == "EQUITY"
    assert rules.trailing_basis == "STATIC"

    start = replace(
        fixture_snapshot(1000),
        cash=Decimal("5000"),
        equity=Decimal("5000"),
        unrealized=Decimal("0"),
    )
    state, reasons = account_step({}, start, rules, 1000)
    assert not reasons
    assert state["trailing_floor"] == "4500"
    assert state["daily_floor"] == "4750"

    rally = replace(
        fixture_snapshot(2000),
        cash=Decimal("5600"),
        equity=Decimal("5600"),
        unrealized=Decimal("0"),
    )
    state, reasons = account_step(state, rally, rules, 2000)
    assert not reasons
    # STATIC max drawdown stays anchored at initial - trailing_loss (4500), never trailing to 5100.
    assert state["trailing_floor"] == "4500"
    assert state["high_water"] == "5000"

    midnight = replace(
        fixture_snapshot(DAY_MS),
        cash=Decimal("5600"),
        equity=Decimal("5600"),
        unrealized=Decimal("0"),
    )
    state, reasons = account_step(state, midnight, rules, DAY_MS)
    assert not reasons
    assert state["trailing_floor"] == "4500"
    assert state["daily_floor"] == "5350"


def test_balance_daily_basis_uses_cash_not_intraday_unrealized_equity() -> None:
    rules = Rulebook.from_challenge("northbook:1step_base:phase_1")
    assert rules.daily_basis == "BALANCE"
    assert rules.trailing_basis == "STATIC"

    snap = replace(
        fixture_snapshot(1000),
        cash=Decimal("5000"),
        equity=Decimal("5200"),
        unrealized=Decimal("200"),
    )
    state, reasons = account_step({}, snap, rules, 1000)
    assert not reasons
    assert state["daily_baseline"] == "5000"
    assert state["daily_floor"] == "4850"
    assert state["trailing_floor"] == "4850"


def test_challenge_rulebook_scaling_and_unknown_key() -> None:
    scaled = Rulebook.from_challenge("hyrotrader:two_step:phase_1", scale=2)
    assert scaled.initial == "10000"
    assert scaled.target == "1000"
    assert scaled.daily_loss == "500"
    assert scaled.trailing_loss == "1000"
    assert scaled.internal_daily_stop == "400"

    with pytest.raises(ValueError, match="UNKNOWN_CHALLENGE_RULEBOOK"):
        Rulebook.from_challenge("nonexistent:program:phase_1")


def test_cli_rulebooks_discovery(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["rulebooks", "--firm", "hyrotrader"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "challenge_rulebooks_v1"
    assert payload["count"] == 3
    assert payload["firms"] == ["hyrotrader"]
    assert payload["trading_authorized"] is False

    code = main(["rulebooks", "--rulebook-key", "hyrotrader:two_step:phase_1", "--scale", "2"])
    assert code == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["schema"] == "challenge_rulebook_detail_v1"
    assert detail["rulebook"]["initial"] == "10000"
    assert detail["rulebook"]["trailing_basis"] == "STATIC"
    assert detail["trading_authorized"] is False


def test_hyrotrader_policy_guards_instrument_symbol_and_low_cap() -> None:
    policy = hyrotrader_policy(fixture_policy(), initial="5000")
    assert policy["low_cap_max_notional"] == "250"

    markets = fixture_markets()
    markets["BTC"]["instrument_type"] = "usdt_perp"
    markets["ETH"]["instrument_type"] = "spot"
    quotes = {
        "BTC": {"bid": "50000", "ask": "50000", "known_at_ms": 1000},
        "ETH": {"bid": "2000", "ask": "2000", "known_at_ms": 1000},
    }
    finance, _ = account_step(
        {},
        replace(
            fixture_snapshot(1000),
            cash=Decimal("5000"),
            equity=Decimal("5000"),
            unrealized=Decimal("0"),
        ),
        Rulebook.from_challenge("hyrotrader:two_step:phase_1"),
        1000,
    )
    pods = [{"pod_id": "p1", "requested_notionals": {"ETH": "100"}, "max_notional": "1000"}]
    blocked_inst = size_book(pods, quotes, markets, policy, finance, {}, {}, [], now_ms=1000)
    assert blocked_inst["status"] == "BLOCKED"
    assert blocked_inst["reasons"] == ["CHALLENGE_INSTRUMENT_PROHIBITED"]

    markets["EUR/USD"] = dict(markets["BTC"], symbol="EUR/USD")
    policy["scenarios"] = [dict(s, **{"EUR/USD": "-0.05"}) for s in policy["scenarios"]]
    quotes["EUR/USD"] = {"bid": "1.1", "ask": "1.1", "known_at_ms": 1000}
    pods_eur = [{"pod_id": "p1", "requested_notionals": {"EUR/USD": "100"}, "max_notional": "1000"}]
    blocked_eur = size_book(pods_eur, quotes, markets, policy, finance, {}, {}, [], now_ms=1000)
    assert blocked_eur["status"] == "BLOCKED"
    assert blocked_eur["reasons"] == ["CHALLENGE_SYMBOL_PROHIBITED"]

    # Low-cap (< $100M mcap) is capped at 5% of $5,000 = $250 notional.
    markets["TINY"] = dict(markets["BTC"], market_cap_usd="50000000", qty_step="1", min_qty="1")
    policy["scenarios"] = [dict(s, TINY="-0.1") for s in policy["scenarios"]]
    quotes["TINY"] = {"bid": "1", "ask": "1", "known_at_ms": 1000}
    pods_tiny = [{"pod_id": "p1", "requested_notionals": {"TINY": "400"}, "max_notional": "1000"}]
    sized_tiny = size_book(pods_tiny, quotes, markets, policy, finance, {}, {}, [], now_ms=1000)
    assert sized_tiny["status"] == "ACCEPTED"
    assert Decimal(sized_tiny["targets"]["TINY"]) <= Decimal("250")

    blocked_tiny = size_book(
        [{"pod_id": "p1", "requested_notionals": {"TINY": "250"}, "max_notional": "1000"}],
        quotes,
        markets,
        policy,
        finance,
        {"TINY": "300"},
        {"p1": {"TINY": "300"}},
        [],
        now_ms=1000,
    )
    assert blocked_tiny["status"] == "NO_TRADE"
    assert "LOW_CAP_EXPOSURE_CAP" in blocked_tiny["reasons"]


def test_consistency_progress_40pct_hyrotrader_rule() -> None:
    res = consistency_progress(
        {"2026-11-10": "350", "2026-11-11": "200", "2026-11-12": "-50"},
        target="500",
        max_single_day_pct=40,
    )
    # 40% of 500 = 200 cap per day -> 200 + 200 - 50 = 350 eligible (< 500 target)
    assert res["total_profit"] == "500"
    assert res["eligible_profit"] == "350"
    assert res["cap_per_day"] == "200"
    assert res["capped_days"] == ["2026-11-10"]
    assert res["target_met"] is False


def test_cft_policy_blocks_usdc_perp_and_spot() -> None:
    policy = challenge_policy("crypto-fund-trader", fixture_policy())
    assert policy == cft_policy(fixture_policy())
    assert policy["allowed_instrument_types"] == ["usdt_perp"]

    markets = fixture_markets()
    markets["BTC"]["instrument_type"] = "usdt_perp"
    markets["ETH"]["instrument_type"] = "usdc_perp"
    quotes = {
        "BTC": {"bid": "50000", "ask": "50000", "known_at_ms": 1000},
        "ETH": {"bid": "2000", "ask": "2000", "known_at_ms": 1000},
    }
    finance, _ = account_step(
        {},
        replace(
            fixture_snapshot(1000),
            cash=Decimal("5000"),
            equity=Decimal("5000"),
            unrealized=Decimal("0"),
        ),
        Rulebook.from_challenge("crypto-fund-trader:two_phase:phase_1"),
        1000,
    )
    blocked_usdc = size_book(
        [{"pod_id": "p1", "requested_notionals": {"ETH": "100"}, "max_notional": "1000"}],
        quotes,
        markets,
        policy,
        finance,
        {},
        {},
        [],
        now_ms=1000,
    )
    assert blocked_usdc["status"] == "BLOCKED"
    assert blocked_usdc["reasons"] == ["CHALLENGE_INSTRUMENT_PROHIBITED"]

    accepted_usdt = size_book(
        [{"pod_id": "p1", "requested_notionals": {"BTC": "100"}, "max_notional": "1000"}],
        quotes,
        markets,
        policy,
        finance,
        {},
        {},
        [],
        now_ms=1000,
    )
    assert accepted_usdt["status"] == "ACCEPTED"
