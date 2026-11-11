"""Code rollback cannot downgrade durable checkpoint/financial interpretations."""

import json

import pytest
from test_release_integrity import layout

from prop_production.deployment import UNITS, activate_release, recover_activation, seal_bundle


def legacy_bundle(path):
    file = path / "compatibility.json"
    body = json.loads(file.read_text())
    contracts = body["contracts"]
    for key in ("portfolio_release_versions", "checkpoint_source", "copy_mandate"):
        contracts.pop(key)
    contracts["source_wire_versions"] = [1, 2]
    file.write_text(json.dumps(body))
    seal_bundle(path)


def test_incompatible_automatic_rollback_stops_without_rewinding(tmp_path):
    opt, units, candidate, previous, journal = layout(tmp_path)
    legacy_bundle(previous)
    floor = tmp_path / "floor"
    floor.write_bytes(b"trusted generation 7; immutable")
    source = tmp_path / "source"
    source.write_bytes(b"checkpoint 91; immutable")
    before = [p.read_bytes() for p in (journal, floor, source)]
    calls = []

    def failed_health():
        raise RuntimeError("failed candidate heartbeat")

    with pytest.raises(ValueError, match="INCOMPATIBLE_DURABLE_CONTRACTS"):
        activate_release(
            opt,
            units,
            candidate,
            previous_active=list(UNITS),
            health_check=failed_health,
            service=calls.append,
        )
    assert (opt / "current").resolve() == candidate
    record = json.loads((opt / "activation.json").read_text())
    assert record["phase"] == "ROLLBACK_FAILED"
    assert record["rollback_transition"]["successor"] == "FENCED"
    assert calls[-1] == ["stop", *UNITS]
    assert [p.read_bytes() for p in (journal, floor, source)] == before
    with pytest.raises(ValueError, match="INCOMPATIBLE_DURABLE_CONTRACTS"):
        recover_activation(opt, units, service=calls.append)
    recover_activation(opt, units, target=candidate, service=calls.append)
    record = json.loads((opt / "activation.json").read_text())
    assert record["phase"] == "ROLLED_BACK"
    assert record["rollback_transition"]["successor"] == "COMPATIBLE"
    assert (opt / "current").resolve() == candidate
    assert [p.read_bytes() for p in (journal, floor, source)] == before


def test_direct_downgrade_cannot_bypass_recovery_guard(tmp_path):
    opt, units, candidate, previous, journal = layout(tmp_path)
    legacy_bundle(candidate)
    calls = []
    before = journal.read_bytes()
    with pytest.raises(ValueError, match="INCOMPATIBLE_DURABLE_CONTRACTS"):
        activate_release(
            opt,
            units,
            candidate,
            previous_active=list(UNITS),
            health_check=lambda: None,
            service=calls.append,
        )
    assert (opt / "current").resolve() == previous
    assert calls == [["stop", *UNITS]]
    assert journal.read_bytes() == before


def test_recovery_target_cannot_escape_release_root(tmp_path):
    opt, units, candidate, previous, journal = layout(tmp_path)
    legacy_bundle(previous)

    def fail():
        raise RuntimeError("synthetic fail")

    with pytest.raises(ValueError):
        activate_release(
            opt,
            units,
            candidate,
            previous_active=list(UNITS),
            health_check=fail,
            service=lambda _: None,
        )
    with pytest.raises(ValueError, match="OUTSIDE_ROOT"):
        recover_activation(opt, units, target=tmp_path, service=lambda _: None)
    assert (opt / "current").resolve() == candidate
