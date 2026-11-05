"""Actual isolated SHADOW workers. No authenticated remote execution mode exists."""
from __future__ import annotations

import base64
import hashlib
import os
import signal
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .contracts import TargetBatchProposal
from .evidence import canonical, digest, load_json, sign
from .finance import Rulebook
from .gateway import BrokerSnapshot, Outcome, snapshot_from_wire
from .guardian import Guardian
from .ipc import LocalClient, LocalServer
from .isolation import restrict_planner
from .journal import Journal
from .judge.broker import FakeBroker
from .planner import Planner
from .release import ReleaseFloor, ReleaseVerifier, fixture_release
from .risk import fixture_markets, fixture_policy


def wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    return value


def public_fixture_key(seed: int, purpose: str) -> Ed25519PrivateKey:
    """Public deterministic synthetic key, accepted only for fixture-account."""
    return Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(f"PUBLIC-SYNTHETIC-ONLY:{seed}:{purpose}".encode()).digest())


def initialize_fixture(root: Path, seed: int = 17) -> dict[str, Any]:
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("guardian", "planner", "broker", "trust", "judge"):
        (root / name).mkdir(mode=0o700)
    approver = public_fixture_key(seed, "approver")
    source = public_fixture_key(seed, "source")
    rules = Rulebook.synthetic()
    policy, markets = fixture_policy(), fixture_markets()
    release = fixture_release()
    release["risk_policy_sha"] = digest({"policy": policy, "markets": markets})
    release["rulebook_sha"] = rules.sha
    envelope = sign(release, approver, "approver")
    settings = {"schema": "synthetic_worker_v1", "release": envelope,
                "approver_public": base64.b64encode(approver.public_key().public_bytes_raw()).decode(),
                "source_public": base64.b64encode(source.public_key().public_bytes_raw()).decode(),
                "policy": policy, "markets": markets, "rules": asdict(rules)}
    config = root / "trust" / "settings.json"
    config.write_bytes(canonical(settings))
    config.chmod(0o600)
    verified = ReleaseVerifier(approver.public_key(), "approver", "fixture-account",
                               release["risk_policy_sha"], rules.sha).verify(envelope, now_ms=1000)
    with ReleaseFloor(root / "trust" / "floor.sqlite", initialize=True) as floor:
        floor.accept(verified)
    with Journal(root / "guardian" / "account.sqlite", "fixture-account", initialize=True):
        pass
    for role in ("guardian", "judge"):
        key = hashlib.sha256(f"PUBLIC-SYNTHETIC-RPC:{seed}:{role}".encode()).digest()
        for folder in (role, "broker"):
            destination = root / folder / (role + ".key")
            destination.write_bytes(key)
            destination.chmod(0o600)
    return {"schema": "fixture_initialized_v1", "source_mode": "SYNTHETIC",
            "release_sha": verified.release_sha, "trading_authorized": False}


class ProcessBrokerGateway:
    synthetic = True

    def __init__(self, client: LocalClient):
        self.client = client

    def _call(self, body: dict[str, Any]) -> dict[str, Any]:
        result = self.client.call(body)
        if result.get("unavailable"):
            raise TimeoutError("SYNTHETIC_PROVIDER_UNAVAILABLE")
        return result

    def observe(self, now_ms: int) -> BrokerSnapshot:
        return snapshot_from_wire(self._call({"command": "observe", "now_ms": now_ms})["snapshot"])

    def submit(self, intent: dict[str, Any]) -> Outcome:
        return Outcome(**self._call({"command": "submit", "intent": intent})["outcome"])

    def cancel(self, intent_id: str) -> Outcome:
        return Outcome(**self._call({"command": "cancel", "intent_id": intent_id})["outcome"])


def _sandbox(root: Path, role: str) -> None:
    # All secrets/config and imports have been loaded before restriction.
    readable = [root / "trust", Path(__file__).parent,
                Path(sys.base_prefix) / "lib", Path(sys.prefix) / "lib"]
    restrict_planner([root / role], list(dict.fromkeys(p.resolve() for p in readable if p.exists())))


def broker_worker(root: Path) -> None:
    broker = FakeBroker(root / "broker" / "truth.sqlite")
    roles = {
        "guardian": (os.getuid(), (root / "broker" / "guardian.key").read_bytes(),
                     {"observe", "submit", "cancel"}),
        "judge": (os.getuid(), (root / "broker" / "judge.key").read_bytes(),
                  {"observe", "fault", "mark", "funding", "ledger", "audit", "fill", "stop"}),
    }
    server = LocalServer(root / "broker" / "b.sock", roles)
    running = True
    def stop(*args: Any) -> None:
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    def handle(role: str, body: dict[str, Any]) -> dict[str, Any]:
        command = body["command"]
        try:
            if command == "observe":
                return {"snapshot": wire(asdict(broker.observe(body["now_ms"])))}
            if command == "submit":
                return {"outcome": asdict(broker.submit(body["intent"]))}
            if command == "cancel":
                return {"outcome": asdict(broker.cancel(body["intent_id"]))}
            if command == "fault":
                allowed = {"fault_mode", "connected", "history_complete", "stale_by_ms", "fill_on_submit"}
                if set(body) - allowed - {"command"}:
                    raise ValueError("FAULT_SCHEMA")
                for k, v in body.items():
                    if k != "command":
                        setattr(broker, k, v)
                return {"configured": True}
            if command == "mark":
                broker.mark(body["market"], body["price"], body["now_ms"])
                return {"marked": True}
            if command == "funding":
                broker.funding(body["amount"], body["now_ms"])
                return {"funding": True}
            if command == "fill":
                broker.fill(body["intent_id"], body["fraction"], body.get("price"))
                return {"filled": True}
            if command == "ledger":
                return {"ledger": broker.ledger()}
            if command == "audit":
                return {"audit": broker.audit()}
            if command == "stop":
                stop()
                return {"stopping": True}
            raise ValueError("COMMAND")
        except TimeoutError:
            return {"unavailable": True}
    _sandbox(root, "broker")
    print('{"ready":"SYNTHETIC_BROKER"}', flush=True)
    try:
        while running:
            server.serve_once(handle)
    finally:
        server.close()
        broker.close()


def financial_worker(root: Path, role: str) -> None:
    settings = load_json((root / "trust" / "settings.json").read_bytes())
    approver = Ed25519PublicKey.from_public_bytes(base64.b64decode(settings["approver_public"]))
    source = Ed25519PublicKey.from_public_bytes(base64.b64decode(settings["source_public"]))
    rules = Rulebook(**settings["rules"])
    if (settings["schema"] != "synthetic_worker_v1" or rules.status != "SYNTHETIC"
            or rules.account_binding != "fixture-account"):
        raise PermissionError("SYNTHETIC_ONLY")
    floor = ReleaseFloor(root / "trust" / "floor.sqlite")
    release = ReleaseVerifier(approver, "approver", "fixture-account",
                               settings["release"]["payload"]["risk_policy_sha"], rules.sha).verify(
                                   settings["release"], now_ms=1000, floor=floor)
    planner: Planner | None = None
    guardian: Guardian | None = None
    journal: Journal | None = None
    if role == "planner":
        planner = Planner(root / "planner" / "receipts.sqlite", release, source)
    else:
        journal = Journal(root / "guardian" / "account.sqlite", "fixture-account")
        client = LocalClient(root / "broker" / "b.sock", "guardian",
                             (root / "guardian" / "guardian.key").read_bytes(), os.getuid())
        guardian = Guardian(journal, release, floor, source, rules, settings["policy"],
                            settings["markets"], ProcessBrokerGateway(client))
    _sandbox(root, role)
    print(canonical({"ready": role.upper(), "trading_authorized": False}).decode(), flush=True)
    try:
        for line in sys.stdin:
            if len(line) > 1000000:
                raise ValueError("INPUT_CAPACITY")
            request = load_json(line)
            command = request["command"]
            if command == "stop":
                break
            try:
                result: dict[str, Any] = {}
                if planner:
                    if command == "ingest":
                        planner.ingest(request["envelope"], local_known_ms=request["local_known_ms"])
                        result = {"ingested": True}
                    elif command == "propose":
                        proposal = planner.propose(request["now_ms"], request["broker_epoch"], request["quotes"])
                        result = {"proposal": proposal.data if proposal else None}
                    else:
                        raise PermissionError("PLANNER_COMMAND")
                elif guardian and journal:
                    now = request.get("now_ms", 0)
                    if command == "reconcile":
                        result = guardian.reconcile(now)
                    elif command == "decide":
                        result = guardian.decide(TargetBatchProposal.from_dict(request["proposal"]),
                                                 request["quotes"], now).data
                    elif command == "reserve":
                        result = {"intent_ids": guardian.reserve(request["decision_id"], now)}
                    elif command == "dispatch":
                        guardian.dispatch(request["intent_id"], now)
                        result = {"dispatched": True}
                    elif command == "cancel":
                        guardian.cancel(request["intent_id"], now)
                        result = {"cancel_requested": True}
                    elif command == "halt":
                        journal.halt("MANUAL_HALT", now)
                        result = {"halted": True}
                    elif command == "status":
                        result = journal.snapshot()
                    elif command == "fault":
                        boundary = request["boundary"]
                        def crash(name: str) -> None:
                            if name == boundary:
                                os.kill(os.getpid(), signal.SIGKILL)
                        guardian.fault = crash
                        journal.fault = crash
                        result = {"scheduled": True}
                    else:
                        raise PermissionError("GUARDIAN_COMMAND")
                print(canonical(result).decode(), flush=True)
            except (ValueError, PermissionError, KeyError):
                print('{"error":"REQUEST_REJECTED"}', flush=True)
    finally:
        if planner:
            planner.close()
        if journal:
            journal.close()
        floor.close()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"broker", "planner", "guardian"}:
        raise SystemExit("Only isolated synthetic workers are supported")
    role, root = sys.argv[1], Path(sys.argv[2]).resolve()
    if role == "broker":
        broker_worker(root)
    else:
        financial_worker(root, role)


if __name__ == "__main__":
    main()
