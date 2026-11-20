"""Declared stress-envelope sizing of the complete book with native lot constraints."""

from __future__ import annotations

from typing import Any

from copytrade_kernel.risk import size_book as size_book
from copytrade_kernel.risk import validate_market_inputs as validate_market_inputs


def fixture_policy() -> dict[str, Any]:
    return {
        "schema": "risk_policy_v1",
        "source_mode": "SYNTHETIC",
        "scale_steps": 1000,
        "minimum_headroom": "10",
        "made_limit": "400",
        "pod_loss_budget": "200",
        "hedge_tolerance": "0.05",
        "shortfall_bps": "30",
        "margin_multiplier": "5",
        "scenarios": [
            {"BTC": "-0.1", "ETH": "-0.15"},
            {"BTC": "0.1", "ETH": "0.15"},
            {"BTC": "-0.1", "ETH": "0.15"},
            {"BTC": "0.1", "ETH": "-0.15"},
        ],
    }


def fixture_markets() -> dict[str, Any]:
    def spec() -> dict[str, Any]:
        return {
            "source_mode": "SYNTHETIC",
            "verified": True,
            "valid_until_ms": 10**15,
            "qty_step": "0.001",
            "price_tick": "0.01",
            "min_qty": "0.001",
            "min_notional": "5",
            "liquidity_notional": "100000",
            "initial_rate": "0.1",
            "fee_rate": "0.001",
            "funding_bound": "0.001",
            "liquidation_rate": "0.005",
            "tiers": [{"cap": "1000000", "rate": "0.01"}],
        }

    return {"BTC": spec(), "ETH": spec()}
