"""Focused tests for kernel sub-min-lot deadbanding in execution.project / plan_execution."""

from __future__ import annotations

from decimal import Decimal

import pytest
from copytrade_kernel import (
    AccountState,
    CheckpointBatch,
    CheckpointMs,
    DecisionInputs,
    DecisionMs,
    FixedKSizing,
    FrozenObject,
    KernelState,
    KnownAtTs,
    MarketContext,
    NativeInstrument,
    OwnerBook,
    RetrospectiveArchive,
    RiskPolicy,
    SourceLeg,
    SourceOutcome,
    VenueCapability,
    step_checkpoint,
)
from copytrade_kernel.execution import project

NOW_MS = 1_800_000_000_000
MARKETS = {
    "BTC": {
        "symbol": "BTC",
        "instrument_type": "perp",
        "verified": True,
        "source_mode": "SYNTHETIC",
        "valid_until_ms": NOW_MS + 10_000_000,
        "qty_step": "0.001",
        "price_tick": "0.1",
        "min_qty": "0.001",
        "min_notional": "10",
        "liquidity_notional": "1000000",
        "initial_rate": "0.5",
        "fee_rate": "0",
        "funding_bound": "0",
        "liquidation_rate": "0",
        "tiers": [{"cap": "1000000", "rate": "0.5"}],
    },
    "ETH": {
        "symbol": "ETH",
        "instrument_type": "perp",
        "verified": True,
        "source_mode": "SYNTHETIC",
        "valid_until_ms": NOW_MS + 10_000_000,
        "qty_step": "0.01",
        "price_tick": "0.1",
        "min_qty": "0.01",
        "min_notional": "10",
        "liquidity_notional": "1000000",
        "initial_rate": "0.5",
        "fee_rate": "0",
        "funding_bound": "0",
        "liquidation_rate": "0",
        "tiers": [{"cap": "1000000", "rate": "0.5"}],
    },
}
QUOTES = {
    "BTC": {"bid": "1000", "ask": "1000", "known_at_ms": NOW_MS - 1000},
    "ETH": {"bid": "100", "ask": "100", "known_at_ms": NOW_MS - 1000},
}
POLICY = {
    "schema": "risk_policy_v1",
    "source_mode": "SYNTHETIC",
    "scale_steps": 4,
    "scenarios": [{"BTC": "-0.10", "ETH": "-0.10"}],
    "minimum_headroom": "50",
    "made_limit": "1000",
    "pod_loss_budget": "400",
    "hedge_tolerance": "0.05",
    "shortfall_bps": "30",
    "margin_multiplier": "1",
    "checkpoint_execution": {
        "quote_max_age_ms": 5000,
        "account_max_age_ms": 5000,
        "max_gross_notional": "20000",
    },
}


def _step_for(
    *,
    btc_target_src_qty: str,
    eth_target_src_qty: str,
    deadband_sub_lot: bool,
    btc_held: str = "0.050",
    eth_held: str = "0",
):
    owner = "owner-1"
    positions = {}
    owner_positions: dict[str, dict[str, str]] = {owner: {}}
    basis = {}
    owner_pos_tuples = []
    if Decimal(btc_held) != 0:
        positions["BTC"] = btc_held
        owner_positions[owner]["BTC"] = btc_held
        basis["BTC"] = {"quantity": btc_held, "entry_price": "1000"}
        owner_pos_tuples.append(("BTC", Decimal(btc_held)))
    if Decimal(eth_held) != 0:
        positions["ETH"] = eth_held
        owner_positions[owner]["ETH"] = eth_held
        basis["ETH"] = {"quantity": eth_held, "entry_price": "100"}
        owner_pos_tuples.append(("ETH", Decimal(eth_held)))

    state = KernelState(owner_books=(OwnerBook(owner, tuple(sorted(owner_pos_tuples)), ()),))
    legs = []
    if Decimal(btc_target_src_qty) != 0:
        legs.append(
            SourceLeg(
                instrument=NativeInstrument("hyperliquid/mainnet:perp:default/BTC"),
                market="BTC",
                quantity=Decimal(btc_target_src_qty),
                mark=Decimal("1000"),
            )
        )
    if Decimal(eth_target_src_qty) != 0:
        legs.append(
            SourceLeg(
                instrument=NativeInstrument("hyperliquid/mainnet:perp:default/ETH"),
                market="ETH",
                quantity=Decimal(eth_target_src_qty),
                mark=Decimal("100"),
            )
        )
    frame = CheckpointBatch(
        checkpoint_ms=CheckpointMs(NOW_MS - 300_000),
        outcomes=(
            SourceOutcome(
                owner=owner,
                sizing=FixedKSizing(Decimal("1")),
                published_at=KnownAtTs(NOW_MS - 300_000),
                deadline=DecisionMs(NOW_MS),
                legs=tuple(legs),
                provenance=RetrospectiveArchive("0" * 16),
            ),
        ),
    )
    inputs = DecisionInputs(
        account=AccountState(
            finance=FrozenObject.parse(
                {"equity": "10000", "headroom": "400", "realized_made": "0", "pod_loss_used": {}}
            ),
            positions=FrozenObject.parse(positions),
            owner_positions=FrozenObject.parse(owner_positions),
            ledger_basis=FrozenObject.parse(basis),
            observed_basis=FrozenObject.parse(basis),
        ),
        market=MarketContext(
            quotes=FrozenObject.parse(QUOTES),
            markets=FrozenObject.parse(MARKETS),
            capabilities=(
                VenueCapability("BTC", True, True),
                VenueCapability("ETH", True, True),
            ),
        ),
        policy=RiskPolicy(FrozenObject.parse(POLICY)),
        now_ms=DecisionMs(NOW_MS),
        deadband_sub_lot=deadband_sub_lot,
    )
    return step_checkpoint(state=state, frame=frame, inputs=inputs)


def test_sub_lot_delta_raises_when_deadband_disabled() -> None:
    # Held BTC=0.050, target BTC=0.055 -> delta=+0.005 BTC = $5 < $10 min_notional.
    step = _step_for(
        btc_target_src_qty="0.055",
        eth_target_src_qty="0.50",
        deadband_sub_lot=False,
    )
    assert step.execution is not None
    assert step.execution.execution_plan is None
    assert step.execution.projection_error == "NATIVE_ADJUSTMENT_LOT"
    assert step.execution.net_orders == ()


def test_sub_lot_delta_holds_position_when_deadband_enabled_without_blocking_other_markets() -> None:
    # Held BTC=0.050, target BTC=0.055 -> deadbanded to 0.050; ETH=0.50 ($50) executes cleanly.
    step = _step_for(
        btc_target_src_qty="0.055",
        eth_target_src_qty="0.50",
        deadband_sub_lot=True,
    )
    assert step.execution is not None
    assert step.execution.projection_error is None
    plan = step.execution.execution_plan.data()
    assert Decimal(plan["targets"]["BTC"]) == Decimal("0.050")
    assert Decimal(plan["targets"]["ETH"]) == Decimal("0.50")
    assert len(step.execution.net_orders) == 1
    assert step.execution.net_orders[0].market == "ETH"
    assert step.execution.net_orders[0].net_delta_qty == Decimal("0.50")


def test_full_close_is_never_deadbanded() -> None:
    # Held BTC=0.005 ($5), target BTC=0 -> full close must NOT be deadbanded, so it still fails closed.
    step = _step_for(
        btc_target_src_qty="0",
        eth_target_src_qty="0.50",
        btc_held="0.005",
        deadband_sub_lot=True,
    )
    assert step.execution is not None
    assert step.execution.execution_plan is None
    assert step.execution.projection_error == "NATIVE_ADJUSTMENT_LOT"


def test_step_size_mismatch_still_raises_even_with_deadband_enabled() -> None:
    # Quantity 0.0055 is not a multiple of qty_step=0.001 -> must still fail closed.
    with pytest.raises(ValueError, match="NATIVE_ADJUSTMENT_LOT"):
        project(
            {
                "positions": {"BTC": "0.050"},
                "pod_positions": {"owner-1": {"BTC": "0.050"}},
                "checkpoint_basis": {"BTC": {"quantity": "0.050", "entry_price": "1000"}},
            },
            {
                "targets": {"BTC": "0.0555"},
                "pod_targets": {"owner-1": {"BTC": "0.0555"}},
            },
            {"position_basis": {"BTC": {"quantity": "0.050", "entry_price": "1000"}}},
            MARKETS,
            QUOTES,
            {},
            permits=lambda _m, _f: True,
            deadband_sub_lot=True,
        )


def test_deadband_restores_non_frozen_owner_omitted_from_pod_targets() -> None:
    plan = project(
        {
            "positions": {"BTC": "0.050"},
            "pod_positions": {"owner-1": {"BTC": "0.030"}, "owner-2": {"BTC": "0.020"}},
            "checkpoint_basis": {"BTC": {"quantity": "0.050", "entry_price": "1000"}},
        },
        {
            "targets": {"BTC": "0.055"},
            # owner-2 is non-frozen but omitted from pod_targets.
            "pod_targets": {"owner-1": {"BTC": "0.055"}},
        },
        {"position_basis": {"BTC": {"quantity": "0.050", "entry_price": "1000"}}},
        MARKETS,
        QUOTES,
        {},
        permits=lambda _m, _f: True,
        deadband_sub_lot=True,
    )
    assert Decimal(plan["targets"]["BTC"]) == Decimal("0.050")
    assert plan["pod_targets"]["owner-1"]["BTC"] == "0.030"
    assert plan["pod_targets"]["owner-2"]["BTC"] == "0.020"
