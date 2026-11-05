from copy import deepcopy
from decimal import Decimal


def inputs():
    from prop_production.finance import Rulebook, account_step
    from prop_production.gateway import fixture_snapshot
    from prop_production.risk import fixture_markets, fixture_policy
    finance, _ = account_step({}, fixture_snapshot(), Rulebook.synthetic(), 1000)
    pods = [{"pod_id": "pod-a", "requested_notionals": {"BTC": "30000"}}]
    quotes = {"BTC": {"bid": "10000", "ask": "10030", "known_at_ms": 1000}}
    return pods, quotes, fixture_markets(), fixture_policy(), finance


def test_15x_request_is_scaled_to_account_and_pod_adverse_budget():
    from prop_production.risk import size_book
    result = size_book(*inputs(), {}, {}, [], now_ms=1000)
    assert result["status"] == "ACCEPTED"
    assert Decimal("0") < Decimal(result["scale"]) < Decimal("0.2")
    assert Decimal(result["worst_loss"]) <= Decimal("390")
    assert Decimal(result["pod_losses"]["pod-a"]) <= Decimal("200")
    assert Decimal(result["targets"]["BTC"]) < Decimal("0.6")


def test_margin_spike_reduces_feasible_book_and_unknown_tier_blocks():
    from prop_production.risk import size_book
    args = list(inputs())
    baseline = size_book(*args, {}, {}, [], now_ms=1000)
    args[2]["BTC"]["tiers"][0]["rate"] = "1"
    args[3]["margin_multiplier"] = "20"
    stressed = size_book(*args, {}, {}, [], now_ms=1000)
    assert Decimal(stressed.get("scale", "0")) < Decimal(baseline["scale"])
    args[2]["BTC"]["tiers"] = []
    assert size_book(*args, {}, {}, [], now_ms=1000)["status"] == "BLOCKED"


def test_native_rounding_never_drops_a_hedge_leg_and_pending_is_reserved():
    from prop_production.risk import size_book
    args = list(inputs())
    args[0][0]["requested_notionals"]["ETH"] = "-15000"
    args[1]["ETH"] = {"bid": "1000", "ask": "1003", "known_at_ms": 1000}
    args[2]["ETH"] = deepcopy(args[2]["BTC"])
    args[2]["ETH"]["min_qty"] = "100"
    assert size_book(*args, {}, {}, [], now_ms=1000)["status"] == "NO_TRADE"
    args = list(inputs())
    result = size_book(*args, {}, {}, [{"market": "BTC", "remaining": "15"}], now_ms=1000)
    assert result["status"] == "NO_TRADE"


def test_correlated_crash_spread_funding_and_partial_hedges_are_included():
    from prop_production.risk import size_book
    args = list(inputs())
    normal = size_book(*args, {}, {}, [], now_ms=1000)
    args[3]["scenarios"] = [{"BTC": "-0.5", "ETH": "-0.5"},
                             {"BTC": "0.5", "ETH": "0.5"}]
    stressed = size_book(*args, {}, {}, [], now_ms=1000)
    assert Decimal(stressed["scale"]) < Decimal(normal["scale"])
    assert Decimal(stressed["cost_reserve"]) > 0
    assert stressed["execution_shortfall_bps"] == "30"
