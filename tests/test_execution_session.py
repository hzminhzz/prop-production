"""Behavioral acceptance: an assessment never grants broker authority."""
from __future__ import annotations

import json

import pytest

from prop_production.cli import main
from prop_production.execution_session import assess


def evidence() -> dict:
    scope = {
        "venue": "BYBIT", "environment": "DEMO", "account_binding": "fixture-demo",
        "release_sha": "a" * 64, "source_scope_sha": "b" * 64,
        "market_scope_sha": "c" * 64,
    }
    return {
        "scope": scope, "capability": "OBSERVE",
        "source": {"scope_sha": scope["source_scope_sha"], "complete": True,
                   "continuous": True, "first_known_proven": True, "heartbeat_ms": 10_000},
        "market": {"scope_sha": scope["market_scope_sha"],
                   "native_identity_verified": True, "quote_ms": 10_000},
        "paper": {"follower_economics_qualified": True, "causal_t300": True,
                  "fixed_k_release_verified": True},
        "broker": {"venue": "BYBIT", "environment": "DEMO", "account_binding": "fixture-demo",
                   "release_sha": scope["release_sha"], "native": True, "synthetic": False,
                   "connection_verified": True, "epoch": 7, "observed_at_ms": 10_000,
                   "positions_complete": True, "pending_orders_complete": True,
                   "history_complete": True, "reconciliation": "COHERENT",
                   "mutation_capable": True},
        "guardian": {"journal_verified": True, "unknown_outcomes": 0, "halts": [],
                     "broker_epoch": 7, "release_sha": scope["release_sha"],
                     "account_binding": "fixture-demo"},
        "risk": {name: True for name in ("equity_verified", "headroom_verified",
                                        "daily_verified", "trailing_verified",
                                        "gross_verified", "pods_verified")},
        "g0_g4_verified": True, "signed_owner_authorization": True,
        "scientifically_qualified": True,
    }


@pytest.mark.parametrize("capability", ("OBSERVE", "PAPER", "DEMO", "LIVE"))
def test_every_capability_is_diagnostic_not_a_permit(capability):
    spec = evidence()
    spec["capability"] = capability
    if capability == "LIVE":
        spec["scope"]["environment"] = "LIVE"
        spec["broker"]["environment"] = "LIVE"
    report = assess(spec, now_ms=10_000)
    assert report["verdict"] == "CLAIMS_COMPLETE_UNVERIFIED"
    assert report["authorization_issued"] is False
    assert report["trading_authorized"] is False
    assert report["mutations"] == 0
    assert report["receipt_sha"] == assess(spec, now_ms=10_000)["receipt_sha"]
    assert "fixture-demo" not in json.dumps(report)


@pytest.mark.parametrize(("path", "replacement", "reason"), [
    (("scope", "environment"), "LIVE", "DEMO_ENVIRONMENT_MISMATCH"),
    (("broker", "account_binding"), "fixture-other", "BROKER_ACCOUNT_BINDING_MISMATCH"),
    (("broker", "epoch"), 8, "GUARDIAN_BROKER_EPOCH_MISMATCH"),
    (("broker", "native"), False, "NATIVE_BROKER_UNVERIFIED"),
    (("broker", "synthetic"), True, "NATIVE_BROKER_UNVERIFIED"),
    (("broker", "connection_verified"), False, "BROKER_CONNECTION_UNVERIFIED"),
    (("broker", "observed_at_ms"), 1, "BROKER_STALE"),
    (("broker", "positions_complete"), False, "BROKER_POSITIONS_INCOMPLETE"),
    (("broker", "history_complete"), False, "BROKER_HISTORY_INCOMPLETE"),
    (("broker", "pending_orders_complete"), False, "BROKER_HISTORY_INCOMPLETE"),
    (("broker", "reconciliation"), "RECONCILIATION_UNCERTAIN", "BROKER_RECONCILIATION_UNCERTAIN"),
    (("broker", "mutation_capable"), False, "BROKER_ORDER_ROUTE_UNAVAILABLE"),
    (("guardian", "unknown_outcomes"), 1, "GUARDIAN_JOURNAL_UNCERTAIN"),
    (("guardian", "halts"), ["BROKER_UNREACHABLE"], "GUARDIAN_HALTED_OR_UNKNOWN"),
    (("guardian", "journal_verified"), False, "GUARDIAN_JOURNAL_UNCERTAIN"),
    (("source", "complete"), False, "SOURCE_INCOMPLETE_OR_GAPPED"),
    (("source", "continuous"), False, "SOURCE_INCOMPLETE_OR_GAPPED"),
    (("source", "heartbeat_ms"), None, "SOURCE_STALE"),
    (("market", "quote_ms"), 1, "MARKET_STALE"),
    (("risk", "equity_verified"), False, "RISK_EQUITY_VERIFIED"),
    (("paper", "fixed_k_release_verified"), False, "FIXED_K_RELEASE_UNVERIFIED"),
    (("paper", "causal_t300"), False, "T300_UNPROVEN"),
    (("signed_owner_authorization",), False, "OWNER_AUTHORIZATION_MISSING"),
    (("g0_g4_verified",), False, "G0_G4_PROOF_MISSING"),
])
def test_adversarial_missing_evidence_blocks(path, replacement, reason):
    spec = evidence()
    spec["capability"] = "DEMO"
    target = spec
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    report = assess(spec, now_ms=10_000)
    assert report["verdict"] == "BLOCKED"
    assert reason in report["blocking_reasons"]
    assert report["mutations"] == 0


def test_missing_input_never_fills_defaults_or_promotes():
    for capability in ("OBSERVE", "PAPER", "DEMO", "LIVE"):
        report = assess({"capability": capability}, now_ms=10_000)
        assert report["verdict"] == "BLOCKED"
        assert report["authorization_issued"] is False
    with pytest.raises(ValueError, match="CAPABILITY_UNKNOWN"):
        assess({"capability": "SUPERUSER"}, now_ms=10_000)


def test_source_only_observe_independent_of_guardian_gates():
    spec = evidence()
    for field in ("paper", "broker", "guardian", "risk",
                  "g0_g4_verified", "signed_owner_authorization"):
        spec.pop(field, None)
    assert assess(spec, now_ms=10_000)["verdict"] == "CLAIMS_COMPLETE_UNVERIFIED"
    spec["capability"] = "DEMO"
    assert assess(spec, now_ms=10_000)["verdict"] == "BLOCKED"


def test_cli_read_only_assessment_returns_nonzero_on_block(tmp_path, capsys):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"capability": "DEMO"}))
    assert main(["session", "--path", str(path), "--now-ms", "10000"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "BLOCKED"
    assert report["mutations"] == 0
