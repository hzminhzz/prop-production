"""Actual SIGKILL at Guardian reservation/send boundaries; independent broker oracle."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest


def crash(root, point):
    from test_checkpoint_admission import request, review
    from test_checkpoint_guardian import system

    fixture = system.__wrapped__(Path(root))
    s = next(fixture)
    m, now, outcome = request(s)
    s.guardian.reconcile(now)
    decision = review(s, m, outcome, now)

    def fault(boundary):
        if boundary == point:
            os.kill(os.getpid(), signal.SIGKILL)

    s.guardian.fault = fault
    ids = s.guardian.reserve_checkpoint(
        decision["decision_id"], receiver=s.receiver, now_ms=now + 1
    )
    s.guardian.dispatch(ids[0], now + 2)
    raise AssertionError("CRASH_BOUNDARY_NOT_REACHED")


@pytest.mark.parametrize(
    "point", ["before_checkpoint_reservation", "after_checkpoint_reservation", "after_send"]
)
def test_checkpoint_sigkill_preserves_one_shot_and_unknown_obligation(tmp_path, point):
    from prop_production.journal import Journal
    from prop_production.judge.broker import FakeBroker

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from test_checkpoint_crash import crash;import sys;crash(*sys.argv[1:])",
            str(tmp_path),
            point,
        ],
        env=os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=15,
    )
    assert result.returncode == -signal.SIGKILL
    journal = Journal(tmp_path / "guardian.sqlite3", "fixture-account")
    broker = FakeBroker(tmp_path / "broker.sqlite3")
    try:
        state = journal.snapshot()
        committed = point != "before_checkpoint_reservation"
        assert len(state["intents"]) == int(committed)
        assert bool(state["reservations"]) == committed
        phases = {row["state"]["phase"] for row in state["checkpoint_opportunities"].values()}
        assert phases == ({"COMMITTED_ONCE"} if committed else {"REVIEWABLE"})
        assert len(broker.audit()) == int(point == "after_send")
        if point == "after_send":
            assert next(iter(state["intents"].values()))["status"] == "UNKNOWN"
            assert state["halts"]
            assert broker.ledger()
        assert state["runtime"]["permit"] is None
        journal.verify()
    finally:
        journal.close()
        broker.close()


def reversal_crash(root, point):
    from checkpoint_scenarios import scenario
    from test_checkpoint_netting import reserve
    from test_checkpoint_reversal import next_decision, opened

    with scenario(Path(root), copies=((0, "0.025"),)) as s:
        opened(s)
        s.wave(2)
        decision = next_decision(s, 3)

        def fault(boundary):
            if boundary == point:
                os.kill(os.getpid(), signal.SIGKILL)

        s.guardian.fault = fault
        ids = reserve(s, decision)
        s.guardian.dispatch(ids[0], s.now + 2)
        raise AssertionError("CRASH_BOUNDARY_NOT_REACHED")


@pytest.mark.parametrize("point", ["before_checkpoint_reservation", "after_send"])
def test_reversal_sigkill_retains_original_owner_basis_and_unknown(tmp_path, point):
    from decimal import Decimal

    from prop_production.journal import Journal
    from prop_production.judge.broker import FakeBroker

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from test_checkpoint_crash import reversal_crash;import sys;reversal_crash(*sys.argv[1:])",
            str(tmp_path),
            point,
        ],
        env=os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=15,
    )
    assert result.returncode == -signal.SIGKILL
    journal = Journal(tmp_path / "guardian.sqlite3", "fixture-account")
    broker = FakeBroker(tmp_path / "broker.sqlite3")
    try:
        state = journal.snapshot()
        assert Decimal(state["positions"]["BTC"]) == Decimal("0.075")
        assert Decimal(state["checkpoint_basis"]["BTC"]["entry_price"]) == Decimal("10030")
        assert state["runtime"]["permit"] is None
        if point == "after_send":
            assert len(state["intents"]) == 2
            assert any(row["status"] == "UNKNOWN" for row in state["intents"].values())
            assert state["reservations"]
            assert state["halts"]
            assert len(broker.audit()) == 2
            assert Decimal(broker.observe(1794442800200).positions[0].quantity) == Decimal("-0.05")
        else:
            assert len(state["intents"]) == 1
            assert not state["reservations"]
            assert len(broker.audit()) == 1
        journal.verify()
    finally:
        journal.close()
        broker.close()
