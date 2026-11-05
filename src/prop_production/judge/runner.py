"""External process coordinator. Only public IPC and read-only journal evidence are used."""
from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..evidence import canonical, digest, load_json, sign
from ..ipc import LocalClient
from ..journal import Journal
from ..worker import initialize_fixture, public_fixture_key
from .reference import ReferenceAccount, check_model, check_protocol


class Worker:
    def __init__(self, role: str, root: Path):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "prop_production.worker", role, str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            close_fds=True, env={"PATH": os.defpath, "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.read()

    def read(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        if not select.select([self.process.stdout], [], [], 10)[0]:
            self.process.kill()
            raise TimeoutError("WORKER_PROGRESS_TIMEOUT")
        line = self.process.stdout.readline()
        if not line:
            assert self.process.stderr is not None
            raise RuntimeError("WORKER_EXIT: " + self.process.stderr.read()[-2000:])
        return dict(load_json(line))

    def call(self, command: str, **kwargs: Any) -> dict[str, Any]:
        assert self.process.stdin is not None
        self.process.stdin.write(canonical({"command": command, **kwargs}).decode() + "\n")
        self.process.stdin.flush()
        return self.read()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(3)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe:
                pipe.close()


def run_fixture(root: Path, *, seed: int = 17, scenario: str = "NORMAL") -> dict[str, Any]:
    if scenario not in {"NORMAL", "ACCEPT_THEN_TIMEOUT", "OUT_OF_ENVELOPE"}:
        raise ValueError("SCENARIO_UNKNOWN")
    initialized = initialize_fixture(root, seed)
    workers = []
    trace: list[dict[str, Any]] = []
    protocol = []
    try:
        broker = Worker("broker", root)
        workers.append(broker)
        control = LocalClient(root / "broker" / "b.sock", "judge",
                              (root / "judge" / "judge.key").read_bytes(), os.getuid())
        guardian = Worker("guardian", root)
        workers.append(guardian)
        planner = Worker("planner", root)
        workers.append(planner)
        trace.append(guardian.call("reconcile", now_ms=301000))
        state = guardian.call("status")
        source = {"schema": "source_book_v1", "source_identity": "fixture-leader",
                  "source_event_ms": 999, "known_at_ms": 1000, "sequence": 1,
                  "scope": ["fixture-leader"], "scope_complete": True, "book_complete": True,
                  "nav": "10000", "positions": [{"market": "BTC", "quantity": "15",
                   "mark": "10000", "mark_known_at_ms": 1000}], "source_mode": "SYNTHETIC"}
        quotes = {"BTC": {"bid": "10000", "ask": "10030", "known_at_ms": 301000}}
        trace.append(planner.call("ingest", envelope=sign(source, public_fixture_key(seed, "source"),
                                                        "source-observer"), local_known_ms=1000))
        proposed = planner.call("propose", now_ms=301000,
                                broker_epoch=state["runtime"]["broker_epoch"], quotes=quotes)
        decision = guardian.call("decide", proposal=proposed["proposal"], quotes=quotes, now_ms=301000)
        trace.extend([proposed, decision])
        reserved = guardian.call("reserve", decision_id=decision["decision_id"], now_ms=301000)
        trace.append(reserved)
        if scenario == "ACCEPT_THEN_TIMEOUT":
            control.call({"command": "fault", "fault_mode": "ACCEPT_THEN_TIMEOUT"})
        for intent_id in reserved["intent_ids"]:
            committed = Journal.read(root / "guardian" / "account.sqlite")
            protocol.append({"kind": "SEND", "intent_id": intent_id,
                "committed": intent_id in committed["intents"],
                "halted": bool(committed["halts"]), "certainty": committed["runtime"]["certainty"],
                "reservation": bool(committed["reservations"]),
                "payload_sha": digest(committed["intents"][intent_id])})
            trace.append(guardian.call("dispatch", intent_id=intent_id, now_ms=301000))
        trace.append(guardian.call("reconcile", now_ms=301001))
        control.call({"command": "funding", "amount": "-1", "now_ms": 301002})
        control.call({"command": "mark", "market": "BTC",
                      "price": "100" if scenario == "OUT_OF_ENVELOPE" else "9900",
                      "now_ms": 301003})
        trace.append(guardian.call("reconcile", now_ms=301004))
        final = guardian.call("status")
        observed = control.call({"command": "observe", "now_ms": 301004})["snapshot"]
        ledger = control.call({"command": "ledger"})["ledger"]
        audit = control.call({"command": "audit"})["audit"]
        oracle = ReferenceAccount("10000")
        for market, price in (("BTC", "10000"), ("ETH", "1000")):
            oracle.apply({"kind": "MARK", "market": market, "price": price, "known_at_ms": 0})
        for event in ledger:
            oracle.apply(event)
        accounting = oracle.compare(observed)
        protocol.append({"kind": "FLAT", "positions": final["positions"],
                         "pending": final["reservations"],
                         "coherent": final["runtime"]["certainty"] == "COHERENT"}) if final["runtime"].get("flat_confirmed") else None
        checked = check_protocol(protocol)
        for submitted in (a for a in audit if a["kind"] == "SUBMIT"):
            if submitted["intent_id"] not in final["intents"] or not submitted["commit_sequence"]:
                checked = {"verdict": "FAIL", "counterexample": submitted}
        report = oracle.report(301004)
        trace.extend([{"ledger": ledger, "audit": audit, "final": final, "reference": report}])
        passed = accounting["verdict"] == "EXACT_REFERENCE_MATCH" and checked["verdict"] == "PROTOCOL_TRACE_VERIFIED"
        result = {
            "schema": "judge_run_v1", "seed": seed, "scenario": scenario,
            "scope": "DECLARED_SYNTHETIC_ENVELOPE",
            "verdict": "BOUNDED_SAFETY_VERIFIED" if passed else "FAIL",
            "accounting": accounting, "protocol": checked, "model": check_model(),
            "market_outcome": "BREACH_OBSERVED" if report["breaches"] else "NO_BREACH_OBSERVED",
            "release_sha": initialized["release_sha"], "trace_sha": digest(trace),
            "sends": len([a for a in audit if a["kind"] == "SUBMIT"]),
            "trading_authorized": False,
        }
        (root / "judge" / "trace.json").write_bytes(canonical(trace))
        (root / "judge" / "verdict.json").write_bytes(canonical(result))
        return result
    finally:
        for worker in reversed(workers):
            worker.close()
