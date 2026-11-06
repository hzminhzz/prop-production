from dataclasses import replace
from decimal import Decimal

import pytest


def test_equity_not_available_balance_and_floors_persist_across_restart():
    from prop_production.finance import Rulebook, account_step
    from prop_production.gateway import fixture_snapshot
    rules = Rulebook.synthetic()
    state, reasons = account_step({}, fixture_snapshot(), rules, 1000)
    assert not reasons
    assert state["daily_floor"] == "9500"
    assert state["internal_floor"] == "9600"
    high = replace(fixture_snapshot(2000), cash=Decimal("10600"), equity=Decimal("10600"))
    state, reasons = account_step(state, high, rules, 2000)
    assert state["trailing_floor"] == "9600"
    low = replace(fixture_snapshot(3000), cash=Decimal("10000"), equity=Decimal("9590"),
                  unrealized=Decimal("-410"))
    state, reasons = account_step(state, low, rules, 3000)
    assert "INTERNAL_DAILY_STOP" in reasons
    assert "TRAILING_FLOOR" in reasons
    assert state["daily_floor"] == "9500"
    assert state["trailing_floor"] == "9600"


def test_midnight_requires_boundary_evidence_and_cannot_lower_trailing_floor():
    from prop_production.finance import Rulebook, account_step
    from prop_production.gateway import fixture_snapshot
    rules = Rulebook.synthetic()
    state, _ = account_step({}, fixture_snapshot(86399000), rules, 86399000)
    blocked, reasons = account_step(state, fixture_snapshot(86410000), rules, 86410000)
    assert "MIDNIGHT_BASELINE_UNKNOWN" in reasons
    assert blocked["day"] == state["day"]
    renewed, reasons = account_step(state, fixture_snapshot(86400000), rules, 86400000)
    assert not reasons
    assert renewed["day"] == 1
    assert Decimal(renewed["trailing_floor"]) >= Decimal(state["trailing_floor"])


def test_rulebook_is_account_bound_and_unauthenticated_rules_block():
    from prop_production.finance import Rulebook, account_step
    from prop_production.gateway import fixture_snapshot
    rules = replace(Rulebook.synthetic(), status="UNVERIFIED")
    _, reasons = account_step({}, fixture_snapshot(), rules, 1000)
    assert "RULEBOOK_UNVERIFIED" in reasons
    with pytest.raises(ValueError, match="ACCOUNT"):
        account_step({}, fixture_snapshot(), replace(rules, account_binding="other"), 1000)
