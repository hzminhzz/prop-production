"""Actual isolated SHADOW workers. No authenticated remote execution mode exists."""

from __future__ import annotations

import base64
import hashlib
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .checkpoint_fixture import INSTRUMENT, POLICY, SCOPE, SOURCE, START
from .checkpoint_receiver import CheckpointSourceReceiver
from .checkpoint_source import integer
from .contracts import (
    PORTFOLIO_RELEASE_V2,
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_ENDPOINT,
)
from .evidence import canonical, digest, load_json, sign
from .finance import Rulebook
from .gateway import BrokerSnapshot, Outcome, snapshot_from_wire
from .guardian import Guardian
from .ipc import LocalClient
from .isolation import restrict_planner
from .journal import Journal
from .release import ReleaseFloor, ReleaseVerifier, fixture_release
from .risk import fixture_markets, fixture_policy


def public_fixture_key(seed: int, purpose: str) -> Ed25519PrivateKey:
    """Public deterministic synthetic key, accepted only for fixture-account."""
    return Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(f"PUBLIC-SYNTHETIC-ONLY:{seed}:{purpose}".encode()).digest()
    )


def initialize_fixture(
    root: Path,
    seed: int = 17,
    *,
    expires_at_ms: int = 900_000,
) -> dict[str, Any]:
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("guardian", "planner", "broker", "trust", "judge"):
        (root / name).mkdir(mode=0o700)
    approver = public_fixture_key(seed, "approver")
    source = public_fixture_key(seed, "source")
    rules = Rulebook.synthetic()
    policy, markets = fixture_policy(), fixture_markets()
    release = fixture_release()
    policy["checkpoint_execution"] = {
        "quote_max_age_ms": 2000,
        "account_max_age_ms": 2000,
        "max_gross_notional": "20000",
    }
    for name, spec in markets.items():
        spec["source_instrument"] = "hyperliquid/mainnet:perp:default/" + name
    markets["BTC"].update(qty_step="0.00001", min_qty="0.00001")
    member = release["members"][0]
    member.update(allowed_markets=[INSTRUMENT], sizing_policy="FIXED_K_NATIVE_QUANTITY", k="0.025")
    release.update(
        schema=PORTFOLIO_RELEASE_V2,
        checkpoint_policy=POLICY,
        expires_at_ms=expires_at_ms,
        checkpoint_scopes={SOURCE: SCOPE},
        mandate_authority_sha=hashlib.sha256(approver.public_key().public_bytes_raw()).hexdigest(),
    )
    release["definition_sha"] = digest(release["members"])
    release["source_sha"] = digest(
        {
            k: release[k]
            for k in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    release["risk_policy_sha"] = digest({"policy": policy, "markets": markets})
    release["rulebook_sha"] = rules.sha
    release["mandates"] = [
        {
            "schema": "copy_mandate_v1",
            "mandate_id": "fixture-copy",
            "generation": 1,
            "source_id": SOURCE,
            "source_scope_generation": SCOPE["generation"],
            "source_account_sha": SCOPE["account_sha"],
            "account_id": SYNTHETIC_ACCOUNT,
            "pod_id": "pod-a",
            "k": "0.025",
            "allowed_markets": [INSTRUMENT],
            "policy_epoch": digest(POLICY),
            "risk_policy_sha": release["risk_policy_sha"],
            "rulebook_sha": rules.sha,
            "effective_from_ms": START,
            "effective_until_ms": expires_at_ms,
            "sizing_policy": "FIXED_K_NATIVE_QUANTITY",
            "entry_policy": "ONE_SHOT_CHECKPOINT_V3",
            "exit_policy": "NATIVE_SAFE_CROSS_OR_REDUCE_V3",
        }
    ]
    envelope = sign(release, approver, "approver")
    settings: dict[str, Any] = {
        "schema": "synthetic_worker_v1",
        "release": envelope,
        "approver_public": base64.b64encode(approver.public_key().public_bytes_raw()).decode(),
        "source_public": base64.b64encode(source.public_key().public_bytes_raw()).decode(),
        "policy": policy,
        "markets": markets,
        "rules": asdict(rules),
    }
    settings["verification_ms"] = 1000
    config = root / "trust" / "settings.json"
    config.write_bytes(canonical(settings))
    config.chmod(0o600)
    verified = ReleaseVerifier(
        approver.public_key(), "approver", SYNTHETIC_ACCOUNT, release["risk_policy_sha"], rules.sha
    ).verify(envelope, now_ms=1000)
    with ReleaseFloor(root / "trust" / "floor.sqlite", initialize=True) as floor:
        floor.accept(verified)
    with Journal(root / "guardian" / "account.sqlite", SYNTHETIC_ACCOUNT, initialize=True):
        pass
    for role in ("guardian", "judge"):
        key = hashlib.sha256(f"PUBLIC-SYNTHETIC-RPC:{seed}:{role}".encode()).digest()
        for folder in (role, "broker"):
            destination = root / folder / (role + ".key")
            destination.write_bytes(key)
            destination.chmod(0o600)
    return {
        "schema": "fixture_initialized_v1",
        "source_mode": "SYNTHETIC",
        "release_sha": verified.release_sha,
        "trading_authorized": False,
    }


class ProcessBrokerGateway:
    synthetic = True
    endpoint = SYNTHETIC_ENDPOINT

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


def sandbox(root: Path, role: str) -> None:
    # All secrets/config and imports have been loaded before restriction.
    readable = [
        root / "trust",
        Path(__file__).parent,
        Path(sys.base_prefix) / "lib",
        Path(sys.prefix) / "lib",
    ]
    restrict_planner(
        [root / role], list(dict.fromkeys(p.resolve() for p in readable if p.exists()))
    )


def financial_worker(root: Path, role: str) -> None:
    settings = load_json((root / "trust" / "settings.json").read_bytes())
    approver = Ed25519PublicKey.from_public_bytes(base64.b64decode(settings["approver_public"]))
    source = Ed25519PublicKey.from_public_bytes(base64.b64decode(settings["source_public"]))
    rules = Rulebook(**settings["rules"])
    if (
        settings["schema"] != "synthetic_worker_v1"
        or rules.status != "SYNTHETIC"
        or rules.account_binding != SYNTHETIC_ACCOUNT
    ):
        raise PermissionError("SYNTHETIC_ONLY")
    if role == "planner":
        raise PermissionError("CHECKPOINTS_BYPASS_LEGACY_PLANNER")
    verification_ms = integer(settings["verification_ms"])
    floor = ReleaseFloor(root / "trust" / "floor.sqlite")
    release = ReleaseVerifier(
        approver,
        "approver",
        SYNTHETIC_ACCOUNT,
        settings["release"]["payload"]["risk_policy_sha"],
        rules.sha,
    ).verify(settings["release"], now_ms=verification_ms, floor=floor)
    journal = Journal(root / "guardian" / "account.sqlite", SYNTHETIC_ACCOUNT)
    client = LocalClient(
        root / "broker" / "b.sock",
        "guardian",
        (root / "guardian" / "guardian.key").read_bytes(),
        os.getuid(),
    )
    guardian = Guardian(
        journal,
        release,
        floor,
        source,
        rules,
        settings["policy"],
        settings["markets"],
        ProcessBrokerGateway(client),
        mandate_key=approver,
    )
    receiver = CheckpointSourceReceiver(
        root / "guardian" / "checkpoint-source.sqlite", release=release, source_key=source
    )
    sandbox(root, role)
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
                now = request.get("now_ms", 0)
                if command == "checkpoint_ingest":
                    result = receiver.ingest(canonical(request["envelope"]), now_ms=now)
                elif command == "checkpoint_status":
                    result = receiver.checkpoint_status()
                elif command == "mandate_activate":
                    result = guardian.activate_copy_mandate(request["envelope"], now_ms=now).data
                elif command == "mandate_control":
                    result = guardian.control_copy_mandate(request["envelope"], now_ms=now).data
                elif command == "mandate_baseline":
                    result = guardian.baseline_copy_mandate(
                        request["owner"], receiver, now_ms=now
                    ).data
                elif command == "checkpoint_review":
                    result = guardian.review_checkpoints(
                        receiver, request["requests"], quotes=request["quotes"], now_ms=now
                    )
                elif command == "checkpoint_reserve":
                    result = {
                        "intent_ids": guardian.reserve_checkpoint(
                            request["decision_id"], receiver=receiver, now_ms=now
                        )
                    }
                elif command == "reconcile":
                    result = guardian.reconcile(now)
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
        receiver.close()
        journal.close()
        floor.close()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"planner", "guardian"}:
        raise SystemExit("Only isolated synthetic workers are supported")
    role, root = sys.argv[1], Path(sys.argv[2]).resolve()
    financial_worker(root, role)


if __name__ == "__main__":
    main()
