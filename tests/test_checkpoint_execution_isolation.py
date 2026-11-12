"""Adversarial market-isolation checks for CWSA execution fallback.

Use independent synthetic broker capability evidence and exact pod targets.
A failed cross-zero claim on one market must never silently change an
unrelated market's independently eligible financial objective.
"""

from decimal import Decimal

import pytest

from prop_production.checkpoint_execution import opening_missed, project
from prop_production.judge.broker import FakeBroker


def projection_proof(tmp_path, *, cross_zero: bool = False):
    broker = FakeBroker(tmp_path / "broker.sqlite3")
    broker.cross_zero_supported = cross_zero
    snapshot = broker.observe(100)
    proof = {
        "endpoint": snapshot.endpoint,
        "execution_capabilities": {
            cap["instrument"]: cap for cap in snapshot.execution_capabilities
        },
        "position_basis": {
            "BTC": {"quantity": "1", "entry_price": "10000"},
            "ETH": {"quantity": "1", "entry_price": "1000"},
        },
    }
    broker.close()
    return proof


def native_markets():
    return {
        market: {"qty_step": "0.00001", "min_qty": "0.00001", "min_notional": "1"}
        for market in ("BTC", "ETH")
    }


def executable_quotes():
    return {
        "BTC": {"bid": "10000", "ask": "10001"},
        "ETH": {"bid": "1000", "ask": "1001"},
    }


def test_unsupported_btc_reversal_does_not_suppress_independent_eth_opening(tmp_path):
    proof = projection_proof(tmp_path)
    state = {
        "account_binding": "fixture-account",
        "positions": {"BTC": "1", "ETH": "0"},
        "pod_positions": {"pod-btc": {"BTC": "1"}, "pod-eth": {}},
        "checkpoint_basis": {"BTC": proof["position_basis"]["BTC"]},
    }
    risk = {
        "targets": {"BTC": "-1", "ETH": "2"},
        "pod_targets": {"pod-btc": {"BTC": "-1"}, "pod-eth": {"ETH": "2"}},
    }
    result = project(
        state,
        risk,
        proof,
        native_markets(),
        executable_quotes(),
        {},
        now_ms=100,
        synthetic=True,
    )
    assert result["mode"] == "REDUCE_ONLY_FALLBACK"
    assert result["opening_status"] == "MISSED"
    assert {key: Decimal(value) for key, value in result["targets"].items()} == {
        "BTC": Decimal("0"),
        "ETH": Decimal("2"),
    }
    assert Decimal(result["pod_targets"]["pod-btc"]["BTC"]) == 0
    assert Decimal(result["pod_targets"]["pod-eth"]["ETH"]) == 2
    assert result["missed_owners"] == ["pod-btc"]


def test_old_generation_eth_residual_only_fences_eth_and_preserves_btc_opening(tmp_path):
    proof = projection_proof(tmp_path, cross_zero=True)
    state = {
        "account_binding": "fixture-account",
        "positions": {"BTC": "0", "ETH": "1"},
        "pod_positions": {"retired-eth": {"ETH": "1"}},
        "checkpoint_basis": {"ETH": proof["position_basis"]["ETH"]},
    }
    risk = {
        "targets": {"BTC": "2", "ETH": "-1"},
        "pod_targets": {
            "retired-eth": {"ETH": "1"},
            "active-btc": {"BTC": "2"},
            "active-eth": {"ETH": "-2"},
        },
    }
    result = project(
        state,
        risk,
        proof,
        native_markets(),
        executable_quotes(),
        {"retired-eth": {"ETH": "1"}},
        now_ms=100,
        synthetic=True,
    )
    assert result["mode"] == "REDUCE_ONLY_FALLBACK"
    assert Decimal(result["targets"]["BTC"]) == 2
    assert Decimal(result["targets"]["ETH"]) == 1
    assert Decimal(result["pod_targets"]["retired-eth"]["ETH"]) == 1
    assert result["missed_owners"] == ["active-eth"]


def test_per_owner_miss_is_explicit_and_tampering_fails_closed(tmp_path):
    proof = projection_proof(tmp_path)
    state = {
        "account_binding": "fixture-account",
        "positions": {"BTC": "1", "ETH": "0"},
        "pod_positions": {"pod-btc": {"BTC": "1"}, "pod-eth": {}},
        "checkpoint_basis": {"BTC": proof["position_basis"]["BTC"]},
    }
    risk = {
        "targets": {"BTC": "-1", "ETH": "2"},
        "pod_targets": {"pod-btc": {"BTC": "-1"}, "pod-eth": {"ETH": "2"}},
    }
    result = project(
        state,
        risk,
        proof,
        native_markets(),
        executable_quotes(),
        {},
        now_ms=100,
        synthetic=True,
    )
    assert opening_missed(result, "pod-btc") is True
    assert opening_missed(result, "pod-eth") is False

    with pytest.raises(ValueError, match="DISPOSITION_PROOF"):
        opening_missed(dict(result, missed_owners=["pod-eth", "pod-btc"]), "pod-btc")
    with pytest.raises(ValueError, match="DISPOSITION_PROOF"):
        opening_missed(dict(result, missed_owners=[]), "pod-btc")


def test_historical_v1_projection_preserves_exact_legacy_shape_and_replay(tmp_path):
    proof = projection_proof(tmp_path)
    state = {
        "account_binding": "fixture-account",
        "positions": {"BTC": "1", "ETH": "0"},
        "pod_positions": {"pod-btc": {"BTC": "1"}, "pod-eth": {}},
        "checkpoint_basis": {"BTC": proof["position_basis"]["BTC"]},
    }
    risk = {
        "targets": {"BTC": "-1", "ETH": "2"},
        "pod_targets": {"pod-btc": {"BTC": "-1"}, "pod-eth": {"ETH": "2"}},
    }
    old = project(
        state,
        risk,
        proof,
        native_markets(),
        executable_quotes(),
        {},
        now_ms=100,
        synthetic=True,
        version="checkpoint_execution_projection_v1",
    )
    assert set(old) == {"schema", "mode", "opening_status", "targets", "pod_targets", "reduce_only"}
    assert old["schema"] == "checkpoint_execution_projection_v1"
    assert old["mode"] == "REDUCE_ONLY_FALLBACK"
    assert {k: Decimal(v) for k, v in old["targets"].items()} == {
        "BTC": Decimal("0"),
        "ETH": Decimal("0"),
    }
    assert opening_missed(old, "pod-btc") is True
    assert opening_missed(old, "pod-eth") is True
