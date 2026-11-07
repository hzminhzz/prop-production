"""Owner-projected dashboard captures are read-only, bounded and non-authorizing."""
from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest
from test_guardian import setup_system
from test_planner import quote


def test_owner_projection_preserves_causal_proposal_and_fills(tmp_path):
    from prop_production.operator import project_state

    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    decision = guardian.decide(proposal, quote(), 301000)
    state = journal.snapshot()
    projected = project_state(state, "portfolio", 301000)
    item = projected["decisions"][0]
    assert item["decision_id"] == decision.data["decision_id"]
    assert item["first_known_ms"] == proposal.data["first_known_ms"]
    assert item["batch_decision_ms"] == proposal.data["decision_ms"]
    assert item["due_ms"] == proposal.data["due_ms"]
    assert item["source_receipts"][0]["source_event_ms"] == 999
    assert item["source_receipts"][0]["local_known_ms"] == 1000
    assert "fixture-leader" not in json.dumps(projected)
    assert "fixture-account" not in json.dumps(projected)
    ids = guardian.reserve(decision.data["decision_id"], 301000)
    guardian.dispatch(ids[0], 301000)
    guardian.reconcile(301001)
    state = journal.snapshot()
    order = project_state(state, "intent", 301001)["intents"][0]
    assert order["intent_id"] == ids[0]
    assert order["decision_id"] == decision.data["decision_id"]
    assert order["filled"] != "0"
    fills = project_state(state, "broker", 301001)["fills"]
    assert fills and fills[0]["intent_id"] == ids[0]
    assert "execution_id" not in fills[0]
    assert fills[0]["execution_id_sha"]
    assert project_state(state, "risk", 301001)["exposure"]["status"] in (
        "VERIFIED_OWNER_MARK_SNAPSHOT", "UNKNOWN"
    )
    journal.close()
    broker.close()
    floor.close()


def test_owner_exposure_unknown_if_mark_or_broker_certainty_missing():
    from prop_production.operator import _exposure

    state = {
        "runtime": {"certainty": "COHERENT", "last_snapshot_ms": 1000},
        "positions": {"BTC": "2", "ETH": "-1"},
        "last_marks": {"BTC": "123.45", "ETH": "100"},
    }
    output = _exposure(state)
    assert output["status"] == "VERIFIED_OWNER_MARK_SNAPSHOT"
    assert Decimal(output["gross"]) == Decimal("346.90")
    assert Decimal(output["net"]) == Decimal("146.90")
    state["last_marks"].pop("ETH")
    assert _exposure(state)["gross"] is None
    state["runtime"]["certainty"] = "UNKNOWN"
    assert _exposure(state)["status"] == "UNKNOWN"


def test_export_single_journal_snapshot_bounded_and_no_financial_write(tmp_path):
    from prop_production.dashboard_capture import COMMANDS, export_capture
    from prop_production.journal import Journal

    journal_path = tmp_path / "journal.sqlite"
    captures = tmp_path / "captures"
    captures.mkdir(mode=0o700)
    with Journal(journal_path, "protected-account", initialize=True) as journal:
        journal.update("RECONCILE", 1000, lambda state: state.update(
            active_release_sha="a" * 64,
            finance={"equity": "0.00", "observed_at_ms": 1000, "risk_floor": "0",
                     "day": 0, "headroom": "0"},
        ))
        original_sequence = journal.sequence
        report = export_capture(journal_path, captures, now_ms=1001)
        assert report["status"] == "FILES_WRITTEN"
        assert journal.sequence == original_sequence
        assert Journal.read(journal_path)["sequence"] == original_sequence
    values = {name: json.loads((captures / f"{name}.json").read_text()) for name in COMMANDS}
    assert len(values) == 6
    assert len({p["native"]["journal_sequence"] for p in values.values()}) == 1
    assert len({p["native"]["account_binding_sha"] for p in values.values()}) == 1
    assert "protected-account" not in json.dumps(values)
    assert values["history"]["native"]["rows"][0]["equity"] == "0.00"
    assert values["risk"]["native"]["finance"]["equity"] == "0.00"
    assert values["risk"]["source_observed_at"].startswith("1970-01-01")
    assert os.stat(captures / "risk.json").st_mode & 0o777 == 0o640


def test_export_refuses_unsafe_capture_root_and_mutable_inputs(tmp_path):
    from prop_production.dashboard_capture import build_captures, export_capture
    from prop_production.journal import Journal

    path = tmp_path / "journal.sqlite"
    with Journal(path, "account", initialize=True):
        pass
    unsafe = tmp_path / "bad"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    with pytest.raises(PermissionError, match="UNTRUSTED"):
        export_capture(path, unsafe, now_ms=1000)
    assert not list(unsafe.iterdir())
    with pytest.raises(ValueError, match="INVALID_CAPTURE_CLOCK"):
        build_captures(Journal.read(path), [], now_ms=-1)
    (tmp_path / "linked").symlink_to(unsafe)
    with pytest.raises(OSError):
        export_capture(path, tmp_path / "linked", now_ms=1000)

def test_public_projection_scrubs_raw_account_and_wallet_identifiers():
    from prop_production.journal import initial_state
    from prop_production.operator import project_state

    account = "private-owner-account"
    wallet = "0x" + "a" * 40
    state = initial_state(account)
    state["sequence"] = 1
    state["decisions"]["p"] = {
        "receipt": {
            "decision_id": "a" * 64, "release_sha": "a" * 64,
            "status": "REJECTED", "reasons": [account + ":" + wallet],
            "requested_pods": [{"pod_id": wallet,
                                "allocation": "100",
                                "requested_notionals": {"BTC": "50"}}],
        },
        "proposal": {},
    }
    projected = project_state(state, "portfolio", 1000)
    serialized = json.dumps(projected)
    assert account not in serialized
    assert wallet not in serialized
    assert "PRIVATE_ACCOUNT" in serialized
    assert "PRIVATE_WALLET" in serialized


def test_specific_old_intent_query_survives_tail_window():
    from prop_production.journal import initial_state
    from prop_production.operator import project_state

    state = initial_state("private-owner-account")
    state["sequence"] = 1
    state["intents"] = {
        f"I{number}": {
            "intent_id": f"I{number}", "decision_id": "D", "release_sha": "a" * 64,
            "market": "BTC", "quantity": "1", "filled": "0", "status": "UNKNOWN",
        }
        for number in range(140)
    }
    general = project_state(state, "intent", 1000)
    assert len(general["intents"]) == 120
    assert general["truncated"]
    specific = project_state(state, "intent", 1000, intent_id="I0")
    assert len(specific["intents"]) == 1
    assert specific["intents"][0]["intent_id"] == "I0"
