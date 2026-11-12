"""The application verifies connector wire evidence without importing the SDK."""

import importlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from prop_production.compatibility import CONNECTOR_COMMIT
from prop_production.gateway import execution_capability
from prop_production.judge.broker import FakeBroker


def verdict(raw, *, now=100, synthetic=True):
    try:
        return execution_capability(
            raw,
            "cross_zero",
            account="fixture-account",
            endpoint="fake://broker",
            instrument="BTC",
            now_ms=now,
            synthetic=synthetic,
        )
    except ValueError:
        return "REJECTED_INVALID_EVIDENCE"


def test_synthetic_broker_and_core_validation_do_not_import_optional_connector(
    tmp_path, monkeypatch
):
    original = importlib.import_module

    def refuse(name, package=None):
        if name.startswith("connectors"):
            raise AssertionError("OPTIONAL_SDK_IMPORTED")
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", refuse)
    broker = FakeBroker(tmp_path / "broker.sqlite3")
    try:
        raw = next(
            r for r in broker.observe(100).execution_capabilities if r["instrument"] == "BTC"
        )
        assert verdict(raw) is True
        assert verdict(raw, synthetic=False) is False
        assert verdict(raw, now=2100) is False
    finally:
        broker.close()


def test_exact_native_connector_and_independent_consumer_agree_on_contract(tmp_path):
    configured = os.environ.get("PROP_CONNECTOR_ROOT")
    if not configured:
        pytest.skip("Set PROP_CONNECTOR_ROOT to the exact connector pin")
    root = Path(configured)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        == CONNECTOR_COMMIT
    )
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip()
    broker = FakeBroker(tmp_path / "broker.sqlite3")
    try:
        original = next(
            r for r in broker.observe(100).execution_capabilities if r["instrument"] == "BTC"
        )
    finally:
        broker.close()
    cases = [original]
    for field, value in (
        ("position_mode", "HEDGE"),
        ("account", "wrong-account"),
        ("known_at_ms", True),
        ("valid_until_ms", 100),
        ("trading_authorized", True),
    ):
        cases.append(dict(deepcopy(original), **{field: value}))
    missing = deepcopy(original)
    missing["cross_zero"]["evidence_sha"] = None
    cases.append(missing)
    unknown = deepcopy(original)
    unknown["cross_zero"] = {"status": "UNKNOWN", "evidence_sha": None, "provenance": None}
    cases.append(unknown)
    script = """
import json,sys
from connectors.execution_capabilities import ExecutionCapabilities
out=[]
for raw in json.load(sys.stdin):
    try:
        out.append(ExecutionCapabilities.parse(raw).permits("cross_zero",account="fixture-account",endpoint="fake://broker",instrument="BTC",now_ms=100,synthetic=True))
    except ValueError:
        out.append("REJECTED_INVALID_EVIDENCE")
print(json.dumps(out))
"""
    observed = subprocess.run(
        [str(root / ".venv/bin/python"), "-c", script],
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        timeout=15,
        cwd=root,
        env={"PATH": os.defpath, "PYTHONPATH": str(root)},
    )
    assert observed.returncode == 0, observed.stderr
    assert json.loads(observed.stdout) == [verdict(raw) for raw in cases]
