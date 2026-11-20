def test_judge_exact_v3(tmp_path):
    from prop_production.judge.runner import run_fixture

    first = run_fixture(tmp_path / "one", seed=17)
    second = run_fixture(tmp_path / "two", seed=17)
    assert first["verdict"] == "BOUNDED_SAFETY_VERIFIED"
    assert first["trace_sha"] == second["trace_sha"]
    assert first["accounting"]["verdict"] == "EXACT_REFERENCE_MATCH"
    assert first["protocol"]["verdict"] == "PROTOCOL_TRACE_VERIFIED"
    assert first["trading_authorized"] is False


def test_judge_v3_failures(tmp_path):
    from prop_production.judge.runner import run_fixture

    timeout = run_fixture(tmp_path / "timeout", seed=18, scenario="ACCEPT_THEN_TIMEOUT")
    assert timeout["verdict"] == "BOUNDED_SAFETY_VERIFIED"
    assert timeout["sends"] == 1
    crash = run_fixture(tmp_path / "gap", seed=19, scenario="OUT_OF_ENVELOPE")
    assert crash["market_outcome"] == "BREACH_OBSERVED"
    assert crash["verdict"] == "NOT_EVALUATED"
    assert crash["trading_authorized"] is False
    assert crash["scope"] == "OUTSIDE_DECLARED_STRESS_ENVELOPE"
