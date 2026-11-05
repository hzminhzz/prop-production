from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_missing_evidence_ci_green_and_ambiguous_beta_never_authorize():
    from prop_production.qualification import demo_report, readiness
    demo = demo_report({"mode": "Beta Trial", "endpoint": "unknown"})
    assert demo["verdict"] == "BLOCKED_DEMO_PROVIDER"
    assert demo["mutations"] == 0
    result = readiness({}, {}, account_binding="fixture-account", release_sha="a" * 64,
                       engine_sha="b" * 64, now_ms=1000)
    assert all(g["verdict"] == "BLOCKED" for g in result["gates"].values())
    assert result["phase_c"] == "BLOCKED"
    assert result["trading_authorized"] is False


def test_independent_scoped_signed_gates_expiry_and_four_week_policy():
    from prop_production.evidence import sign
    from prop_production.qualification import readiness
    keys = {gate: Ed25519PrivateKey.generate() for gate in ("G0", "G1", "G2", "G3", "G4")}
    evidence = {}
    for gate, key in keys.items():
        claims = {
            "inputs_complete": True, "causal_source_scope": True, "rulebook_authenticated": True,
            "journal_verified": True, "coherent_provider_cutoff": True, "no_unknown_orders": True,
            "safety_verdict": "BOUNDED_SAFETY_VERIFIED", "negative_controls_pass": True,
            "independent_oracle": True, "paper_days": 28, "gap_free": True,
            "economics_verdict": "QUALIFIED_OOS", "independent_follower_pnl": True,
            "provider_demo_verified": True, "explicit_owner_authorization": True,
            "risk_limits_verified": True, "authorization_nonce": "new-approval-1",
        }
        body = {"schema": "readiness_attestation_v1", "gate": gate,
                "account_binding": "fixture-account", "release_sha": "a" * 64,
                "engine_sha": "b" * 64, "created_at_ms": 100, "expires_at_ms": 10000,
                "evidence_sha": "c" * 64, "claims": claims}
        evidence[gate] = sign(body, key, "reviewer-" + gate)
    trust = {g: {"key": k.public_key(), "authority": "reviewer-" + g} for g, k in keys.items()}
    result = readiness(evidence, trust, account_binding="fixture-account", release_sha="a" * 64,
                       engine_sha="b" * 64, now_ms=1000)
    assert result["gates"]["G3"]["verdict"] == "APPROVED"
    assert result["authorization_evidence"] == "APPROVED"
    assert result["trading_authorized"] is False  # Evaluation is not a mutation capability.
    assert readiness(evidence, trust, account_binding="wrong", release_sha="a" * 64,
                     engine_sha="b" * 64, now_ms=1000)["phase_c"] == "BLOCKED"
    assert readiness(evidence, trust, account_binding="fixture-account", release_sha="a" * 64,
                     engine_sha="b" * 64, now_ms=10001)["phase_c"] == "BLOCKED"
    assert readiness(evidence, trust, account_binding="fixture-account", release_sha="a" * 64,
                     engine_sha="b" * 64, now_ms=1000,
                     revoked_nonces={"new-approval-1"})["phase_c"] == "BLOCKED"
    body = evidence["G3"]["payload"]
    body["claims"]["paper_days"] = 27
    evidence["G3"] = sign(body, keys["G3"], "reviewer-G3")
    assert readiness(evidence, trust, account_binding="fixture-account", release_sha="a" * 64,
                     engine_sha="b" * 64, now_ms=1000)["gates"]["G3"]["verdict"] == "BLOCKED"


def test_watchdog_requires_financial_progress_even_if_process_is_alive():
    from prop_production.health import assess_health
    result = assess_health({"runtime": {"last_progress_ms": 0, "last_snapshot_ms": 0,
                                       "certainty": "COHERENT", "flat_confirmed": True},
                            "halts": [], "finance": {"equity": "10000"}},
                           now_ms=45000, source={"connected": True, "heartbeat_ms": 45000,
                                                "last_event_ms": 0, "book_known_ms": 45000},
                           market_ms=45000, watchdog_ms=45000)
    assert result["status"] == "BLOCKED"
    assert result["source_state"] == "QUIET"
    assert "FINANCIAL_PROGRESS_STALE" in result["reasons"]
    assert result["flat_confirmed"] is False
