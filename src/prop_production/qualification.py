"""Deterministic signed evidence gates. Evaluation never creates a trading capability."""
from __future__ import annotations

from typing import Any

from .evidence import digest, require_sha, verify

CONNECTOR_COMMIT = "b28aa8522c51830a4b389a61b20ceb539b7a89ca"
FOUR_WEEKS_MS = 28 * 86400000
REQUIREMENTS: dict[str, dict[str, Any]] = {
    "G0": {"inputs_complete": True, "causal_source_scope": True, "rulebook_authenticated": True},
    "G1": {"journal_verified": True, "coherent_provider_cutoff": True, "no_unknown_orders": True},
    "G2": {"safety_verdict": "BOUNDED_SAFETY_VERIFIED", "negative_controls_pass": True,
           "independent_oracle": True},
    "G3": {"gap_free": True, "economics_verdict": "QUALIFIED_OOS", "independent_follower_pnl": True},
    "G4": {"provider_demo_verified": True, "explicit_owner_authorization": True,
           "risk_limits_verified": True},
}


def demo_report(classification: dict[str, Any] | None = None, *, trust: dict[str, Any] | None = None,
                account_binding: str = "UNKNOWN", endpoint: str = "UNKNOWN",
                now_ms: int = 0) -> dict[str, Any]:
    unknowns = ["EXACT_ACCOUNT_CLASSIFICATION", "RISK_FREE_POST_PROOF", "COSTS",
                "REDUCE_ONLY", "STOPS", "CANCEL_FILL_RACES", "MARGIN_TIERS",
                "COHERENT_PROVIDER_CUTOFF", "EXPLICIT_MUTATION_AUTHORIZATION"]
    if classification and trust:
        try:
            body = verify(classification, trust["key"], trust["authority"])
            if (body["account_binding"] != account_binding or body["endpoint"] != endpoint
                    or body["mode"] != "ZERO_CAPITAL_DEMO" or body["risk_free_post"] is not True
                    or not body["created_at_ms"] <= now_ms < body["expires_at_ms"]):
                raise ValueError("PROVIDER_SCOPE")
            unknowns = [name for name in unknowns if body["verified_semantics"].get(name) is not True]
        except (ValueError, KeyError, TypeError):
            unknowns.append("CLASSIFICATION_SIGNATURE_OR_SCOPE")
    # No remote capability is implemented or granted by this report.
    return {"schema": "demo_qualification_v1",
            "verdict": "BLOCKED_DEMO_PROVIDER" if unknowns else "BLOCKED_DEMO_CANARY_AUTHORIZATION",
            "unknowns": sorted(set(unknowns)), "connector_commit": CONNECTOR_COMMIT,
            "classification_sha": digest(classification) if classification else None,
            "canary_evidence": "NOT_EVALUATED", "mutations": 0,
            "trading_authorized": False, "phase_c_authorized": False}


def readiness(evidence: dict[str, Any], trust: dict[str, Any], *,
              account_binding: str, release_sha: str, engine_sha: str, now_ms: int,
              revoked_nonces: set[str] | None = None,
              minimum_authorization_ms: int = 0) -> dict[str, Any]:
    require_sha(release_sha)
    require_sha(engine_sha)
    gates = {}
    used_keys: set[bytes] = set()
    revoked_nonces = revoked_nonces or set()
    for gate, requirements in REQUIREMENTS.items():
        reasons = []
        details: dict[str, Any] = {}
        try:
            authority = trust[gate]
            public = authority["key"].public_bytes_raw()
            if public in used_keys:
                raise ValueError("REVIEW_AUTHORITIES_NOT_INDEPENDENT")
            used_keys.add(public)
            body = verify(evidence[gate], authority["key"], authority["authority"])
            expected = {"schema", "gate", "account_binding", "release_sha", "engine_sha",
                        "created_at_ms", "expires_at_ms", "evidence_sha", "claims"}
            if set(body) != expected or body["schema"] != "readiness_attestation_v1" or body["gate"] != gate:
                raise ValueError("ATTESTATION_SCHEMA")
            if (body["account_binding"] != account_binding or body["release_sha"] != release_sha
                    or body["engine_sha"] != engine_sha):
                raise ValueError("ATTESTATION_SCOPE")
            if (type(body["created_at_ms"]) is not int or type(body["expires_at_ms"]) is not int
                    or not 0 <= body["created_at_ms"] <= now_ms < body["expires_at_ms"]):
                raise ValueError("ATTESTATION_EXPIRED_OR_FUTURE")
            require_sha(body["evidence_sha"])
            claims = body["claims"]
            for name, expected_value in requirements.items():
                if type(claims.get(name)) is not type(expected_value) or claims.get(name) != expected_value:
                    reasons.append("MISSING_" + name.upper())
            if gate == "G3" and (type(claims.get("paper_days")) is not int or claims["paper_days"] < 28):
                reasons.append("FOUR_WEEK_PAPER_MINIMUM")
            if gate == "G4":
                nonce = claims.get("authorization_nonce")
                if (not isinstance(nonce, str) or not nonce or nonce in revoked_nonces
                        or body["created_at_ms"] < minimum_authorization_ms):
                    reasons.append("AUTHORIZATION_REPLAY_OR_REVOCATION")
            details = {"authority": authority["authority"], "evidence_sha": body["evidence_sha"],
                       "created_at_ms": body["created_at_ms"], "expires_at_ms": body["expires_at_ms"]}
        except (ValueError, KeyError, TypeError):
            reasons.append("MISSING_INVALID_OR_UNTRUSTED_ATTESTATION")
        gates[gate] = {"verdict": "BLOCKED" if reasons else "APPROVED",
                       "reasons": sorted(set(reasons)), **details}
    approved = all(g["verdict"] == "APPROVED" for g in gates.values())
    return {
        "schema": "readiness_dossier_v1", "gates": gates,
        "account_binding_sha": digest(account_binding), "release_sha": release_sha,
        "engine_sha": engine_sha, "known_at_ms": now_ms,
        "phase_c": "EVIDENCE_APPROVED_EXECUTION_DISABLED" if approved else "BLOCKED",
        "authorization_evidence": gates["G4"]["verdict"],
        "required_paper_days": 28, "connector_commit": CONNECTOR_COMMIT,
        "statuses": {"IMPLEMENTED": True, "ENGINEERING_VERIFIED": gates["G2"]["verdict"] == "APPROVED",
                     "DEPLOYED": False, "RUNNING_OBSERVED": False,
                     "SCIENTIFICALLY_QUALIFIED": gates["G3"]["verdict"] == "APPROVED",
                     "TRADING_AUTHORIZED": False},
        "trading_authorized": False,
    }
