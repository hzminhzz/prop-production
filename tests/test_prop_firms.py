"""Tests for canonical prop-firm challenge Rulebook catalog, STATIC/BALANCE math, and CLI."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from prop_production.cli import main
from prop_production.finance import DAY_MS, Rulebook, account_step
from prop_production.gateway import fixture_snapshot
from prop_production.prop_firms import CHALLENGE_RULEBOOKS, get_challenge_spec


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
