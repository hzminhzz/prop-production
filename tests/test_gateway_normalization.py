"""Provider status is not financial proof without causal cumulative executions."""

from decimal import Decimal

import pytest

from prop_production.gateway import normalize_broker_status


@pytest.mark.parametrize("provider", ["propr", "bybit_demo"])
def test_terminal_requires_history_and_actual_cumulative_fill(provider):
    bad = normalize_broker_status(
        provider,
        "filled",
        quantity=Decimal("10"),
        cumulative_filled=Decimal("6"),
        complete_history=True,
        executions_reconciled=True,
    )
    assert bad.status == "UNKNOWN"
    assert bad.raw_status == "filled"
    assert bad.provenance == "FALSE_FILLED_TERMINAL"
    assert bad.schema == "broker_status_normalization_v1"
    missing = normalize_broker_status(
        provider,
        "filled",
        quantity=Decimal("10"),
        cumulative_filled=Decimal("10"),
        complete_history=False,
        executions_reconciled=False,
    )
    assert missing.status == "UNKNOWN"
    assert missing.provenance == "TERMINAL_EVIDENCE_UNQUALIFIED"
    valid = normalize_broker_status(
        provider,
        "filled",
        quantity=Decimal("10"),
        cumulative_filled=Decimal("10"),
        complete_history=True,
        executions_reconciled=True,
    )
    assert valid.status == "FILLED"
    assert valid.provenance == "PROVIDER_CUTOFF_AND_EXECUTIONS_VERIFIED"


def test_unknown_provider_status_and_nonterminal_partial_are_accountable():
    partial = normalize_broker_status(
        "bybit_demo",
        "partially_filled",
        quantity=Decimal("-5"),
        cumulative_filled=Decimal("-2"),
        complete_history=False,
        executions_reconciled=False,
    )
    assert partial.status == "PARTIAL"
    assert partial.raw_status == "partially_filled"
    unknown = normalize_broker_status(
        "propr",
        "DELAYED_SPECIAL_TERMINAL",
        quantity=Decimal("5"),
        cumulative_filled=Decimal("0"),
        complete_history=True,
        executions_reconciled=False,
    )
    assert unknown.status == "UNKNOWN"
    assert unknown.provenance == "UNKNOWN_PROVIDER_STATUS"


@pytest.mark.parametrize("filled", [Decimal("-2"), Decimal("11")])
def test_negative_or_overfill_not_normalized(filled):
    with pytest.raises(ValueError, match="NORMALIZATION_INPUT"):
        normalize_broker_status(
            "propr",
            "filled",
            quantity=Decimal("10"),
            cumulative_filled=filled,
            complete_history=True,
            executions_reconciled=True,
        )
