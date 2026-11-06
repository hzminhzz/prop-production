import ast
import inspect
from fractions import Fraction


def test_independent_exact_accounting_fees_funding_and_midnight():
    from prop_production.judge.reference import ReferenceAccount
    oracle = ReferenceAccount("10000")
    oracle.apply({"kind": "MARK", "market": "BTC", "price": "10000", "known_at_ms": 1000})
    oracle.apply({"kind": "FILL", "execution_id": "1", "market": "BTC", "quantity": "3",
                  "price": "10030", "fee": "30.09", "known_at_ms": 1001})
    oracle.apply({"kind": "FUNDING", "amount": "-5", "known_at_ms": 2000})
    assert oracle.cash == Fraction("9964.91")
    assert oracle.equity == Fraction("9874.91")
    oracle.apply({"kind": "MARK", "market": "BTC", "price": "8000", "known_at_ms": 3000})
    assert oracle.equity == Fraction("3874.91")
    assert "CONTRACT_DAILY_FLOOR" in oracle.report(3000)["breaches"]
    midnight = oracle.report(86400000)
    assert Fraction(midnight["trailing_floor"]) == Fraction("9000")
    assert midnight["trading_authorized"] is False


def test_oracle_detects_negative_accounting_and_permission_controls():
    from prop_production.judge.reference import ReferenceAccount, check_protocol
    oracle = ReferenceAccount("10000")
    assert oracle.compare({"cash": "9999", "equity": "10000"})["verdict"] == "FAIL"
    bad = [{"kind": "SEND", "intent_id": "a", "committed": False, "halted": False,
            "certainty": "COHERENT", "payload_sha": "1", "reservation": True}]
    assert check_protocol(bad)["verdict"] == "FAIL"
    for field, value in (("halted", True), ("certainty", "UNKNOWN"), ("reservation", False)):
        event = dict(bad[0], committed=True)
        event[field] = value
        assert check_protocol([event])["verdict"] == "FAIL"
    repeated = dict(bad[0], committed=True)
    assert check_protocol([repeated, dict(repeated, payload_sha="2")])["verdict"] == "FAIL"


def test_oracle_never_imports_guardian_financial_or_risk_functions():
    from prop_production.judge import reference
    tree = ast.parse(inspect.getsource(reference))
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert all(not name or name.split(".")[0] in
               {"__future__", "fractions", "typing", "dataclasses", "collections"} for name in imports)


def test_outside_judge_rejects_guardian_projection_mutants():
    from prop_production.judge.reference import check_guardian_projection

    broker = {"positions": [{"market": "BTC", "quantity": "1"}]}
    reference = {"cash": "9990", "equity": "9990", "breaches": []}
    guardian = {"finance": {"cash": "9990", "equity": "9990"},
                "positions": {"BTC": "1"}, "pod_positions": {"pod-a": {"BTC": "1"}},
                "runtime": {"certainty": "COHERENT", "flat_confirmed": False},
                "reservations": {}, "halts": []}
    assert check_guardian_projection(guardian, broker, reference)["verdict"] == "GUARDIAN_REFERENCE_MATCH"
    assert check_guardian_projection(dict(guardian, finance={"cash": "9991", "equity": "9990"}),
                                    broker, reference)["verdict"] == "FAIL"
    assert check_guardian_projection(dict(guardian, pod_positions={"pod-a": {"BTC": "2"}}),
                                    broker, reference)["verdict"] == "FAIL"
    assert check_guardian_projection(dict(guardian, runtime={"certainty": "COHERENT",
                                                             "flat_confirmed": True}),
                                    broker, reference)["verdict"] == "FAIL"
    assert check_guardian_projection(guardian, broker, dict(reference,
                                    breaches=["TRAILING_FLOOR"]))["verdict"] == "FAIL"


def test_bounded_protocol_model_explores_crash_and_unknown_states():
    from prop_production.judge.reference import check_model
    result = check_model(10)
    assert result["verdict"] == "BOUNDED_PROTOCOL_VERIFIED"
    assert result["states"] >= 20
    assert result["depth"] == 10
