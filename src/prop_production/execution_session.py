"""Read-only, account-bound execution-session evidence assessment.

The session is a proof-carrying diagnostic, not a permit or financial writer.
Only the existing Guardian may commit and dispatch a broker intent.
"""
from __future__ import annotations

from typing import Any

from .evidence import digest

CAPABILITIES = ("OBSERVE", "PAPER", "DEMO", "LIVE")
MAX_AGE_MS = {"source": 30_000, "market": 5_000, "broker": 5_000}
REQUIRED_SCOPES = ("venue", "environment", "account_binding", "release_sha",
                   "source_scope_sha", "market_scope_sha")
MISSING = object()


def _fresh(value: object, now_ms: int, maximum: int) -> bool:
    return type(value) is int and 0 <= now_ms - value <= maximum


def assess(spec: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Produce an immutable-content, versioned assessment, never an order grant.

    All claims are explicitly supplied by their owning evidence adapter.
    Absence, inconsistent identity, stale timestamps and ambiguous capability
    fail closed. A caller cannot promote a synthetic/mock account to DEMO/LIVE.
    """
    if not isinstance(spec, dict) or type(now_ms) is not int or now_ms < 0:
        raise ValueError("SESSION_SPEC_OR_CLOCK")
    scope = spec.get("scope")
    if not isinstance(scope, dict):
        scope = {}
    requested = spec.get("capability", "OBSERVE")
    if requested not in CAPABILITIES:
        raise ValueError("CAPABILITY_UNKNOWN")
    reasons: set[str] = set()
    for name in REQUIRED_SCOPES:
        if not isinstance(scope.get(name), str) or not scope[name].strip():
            reasons.add("SCOPE_" + name.upper() + "_MISSING")
    environment = scope.get("environment")
    if requested == "LIVE" and environment != "LIVE":
        reasons.add("LIVE_ENVIRONMENT_MISMATCH")
    if requested == "DEMO" and environment != "DEMO":
        reasons.add("DEMO_ENVIRONMENT_MISMATCH")

    source_input = spec.get("source")
    market_input = spec.get("market")
    source: dict[str, Any] = source_input if isinstance(source_input, dict) else {}
    market: dict[str, Any] = market_input if isinstance(market_input, dict) else {}
    if source.get("scope_sha") != scope.get("source_scope_sha"):
        reasons.add("SOURCE_SCOPE_MISMATCH")
    if source.get("complete") is not True or source.get("continuous") is not True:
        reasons.add("SOURCE_INCOMPLETE_OR_GAPPED")
    if source.get("first_known_proven") is not True:
        reasons.add("SOURCE_FIRST_KNOWN_UNPROVEN")
    if not _fresh(source.get("heartbeat_ms"), now_ms, MAX_AGE_MS["source"]):
        reasons.add("SOURCE_STALE")
    if market.get("scope_sha") != scope.get("market_scope_sha"):
        reasons.add("MARKET_SCOPE_MISMATCH")
    if market.get("native_identity_verified") is not True:
        reasons.add("MARKET_IDENTITY_UNVERIFIED")
    if not _fresh(market.get("quote_ms"), now_ms, MAX_AGE_MS["market"]):
        reasons.add("MARKET_STALE")
    # Read-only source receipts may support OBSERVE without broker credentials.
    if requested != "OBSERVE":
        paper_input = spec.get("paper")
        paper: dict[str, Any] = paper_input if isinstance(paper_input, dict) else {}
        if paper.get("follower_economics_qualified") is not True:
            reasons.add("FOLLOWER_ECONOMICS_UNQUALIFIED")
        if paper.get("causal_t300") is not True:
            reasons.add("T300_UNPROVEN")
        if paper.get("fixed_k_release_verified") is not True:
            reasons.add("FIXED_K_RELEASE_UNVERIFIED")

    if requested in ("DEMO", "LIVE"):
        broker_input = spec.get("broker")
        guardian_input = spec.get("guardian")
        risk_input = spec.get("risk")
        broker: dict[str, Any] = broker_input if isinstance(broker_input, dict) else {}
        guardian: dict[str, Any] = guardian_input if isinstance(guardian_input, dict) else {}
        risk: dict[str, Any] = risk_input if isinstance(risk_input, dict) else {}
        for key in ("venue", "environment", "account_binding", "release_sha"):
            if broker.get(key) != scope.get(key):
                reasons.add("BROKER_" + key.upper() + "_MISMATCH")
        if broker.get("native") is not True or broker.get("synthetic") is not False:
            reasons.add("NATIVE_BROKER_UNVERIFIED")
        if broker.get("connection_verified") is not True or type(broker.get("epoch")) is not int:
            reasons.add("BROKER_CONNECTION_UNVERIFIED")
        if not _fresh(broker.get("observed_at_ms"), now_ms, MAX_AGE_MS["broker"]):
            reasons.add("BROKER_STALE")
        if broker.get("positions_complete") is not True:
            reasons.add("BROKER_POSITIONS_INCOMPLETE")
        if broker.get("pending_orders_complete") is not True or broker.get("history_complete") is not True:
            reasons.add("BROKER_HISTORY_INCOMPLETE")
        if broker.get("reconciliation") != "COHERENT":
            reasons.add("BROKER_RECONCILIATION_UNCERTAIN")
        if broker.get("mutation_capable") is not True:
            reasons.add("BROKER_ORDER_ROUTE_UNAVAILABLE")
        if guardian.get("journal_verified") is not True or guardian.get("unknown_outcomes") != 0:
            reasons.add("GUARDIAN_JOURNAL_UNCERTAIN")
        if guardian.get("halts") != []:
            reasons.add("GUARDIAN_HALTED_OR_UNKNOWN")
        if guardian.get("broker_epoch") != broker.get("epoch"):
            reasons.add("GUARDIAN_BROKER_EPOCH_MISMATCH")
        if guardian.get("release_sha") != scope.get("release_sha"):
            reasons.add("GUARDIAN_RELEASE_MISMATCH")
        if guardian.get("account_binding") != scope.get("account_binding"):
            reasons.add("GUARDIAN_ACCOUNT_MISMATCH")
        for name in ("equity_verified", "headroom_verified", "daily_verified",
                     "trailing_verified", "gross_verified", "pods_verified"):
            if risk.get(name) is not True:
                reasons.add("RISK_" + name.upper())
        if spec.get("signed_owner_authorization") is not True:
            reasons.add("OWNER_AUTHORIZATION_MISSING")
        # A signed G0-G4 dossier is an independent authority, not inferred from
        # individual flags or the existence of this diagnostic receipt.
        if spec.get("g0_g4_verified") is not True:
            reasons.add("G0_G4_PROOF_MISSING")
        if requested == "LIVE" and spec.get("scientifically_qualified") is not True:
            reasons.add("LIVE_SCIENTIFIC_QUALIFICATION_MISSING")

    # Never include raw provider identifiers or private financial evidence in
    # an operator-facing receipt; hashes bind the full supplied input content.
    evidence_ids = {name: digest(spec.get(name)) for name in
                    ("source", "market", "paper", "broker", "guardian", "risk")}
    body: dict[str, Any] = {
        "schema": "execution_session_assessment_v1",
        "capability": requested,
        "scope_sha": digest(scope),
        "evidence_sha": digest(spec),
        "evidence_ids": evidence_ids,
        "known_at_ms": now_ms,
        "expires_at_ms": now_ms + MAX_AGE_MS["market"],
        "verdict": "CLAIMS_COMPLETE_UNVERIFIED" if not reasons else "BLOCKED",
        "trust_status": "UNVERIFIED_INPUT_NOT_AUTHORITY",
        "blocking_reasons": sorted(reasons),
        "authorization_issued": False,
        "trading_authorized": False,
        "mutations": 0,
    }
    body["receipt_sha"] = digest(body)
    return body
