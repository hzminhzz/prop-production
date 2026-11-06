"""Credential-free operator entrypoint. Live execution is deliberately unavailable."""
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__


def status() -> dict[str, Any]:
    return {"schema": "operator_status_v1", "version": __version__, "mode": "SHADOW",
            "state": "DISARMED", "account_readiness": "UNKNOWN", "order_route": "NONE",
            "trading_authorized": False, "scientifically_qualified": False}


def _command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    now_ms = args.now_ms if args.now_ms is not None else time.time_ns() // 1000000
    if args.command in {"status", "smoke"} and not args.journal:
        result = status()
        if args.command == "smoke":
            result.update(synthetic=True, equity="10000", source="FIXTURE")
        return result, 0
    if args.command in {"status", "portfolio", "risk", "broker", "intent"}:
        from .operator import read_model
        if args.journal is None:
            raise ValueError("JOURNAL_REQUIRED")
        return read_model(args.journal, args.command, now_ms, args.intent_id), 0
    if args.command == "halt":
        from .journal import Journal
        if args.journal is None:
            raise ValueError("JOURNAL_REQUIRED")
        # Exclusive writer fencing refuses an offline edit while a Guardian is running.
        account = Journal.read(args.journal)["account_binding"]
        with Journal(args.journal, account) as journal:
            journal.halt("MANUAL_HALT", now_ms)
            return {"schema": "operator_halt_v1", "state": "HALTED",
                    "journal_sequence": journal.sequence, "trading_authorized": False}, 0
    if args.command == "fixture" and args.action == "init":
        from .worker import initialize_fixture
        if args.directory is None:
            raise ValueError("DIRECTORY_REQUIRED")
        return initialize_fixture(args.directory, args.seed), 0
    if args.command == "judge":
        if args.action == "run":
            from .judge.runner import run_fixture
            if args.directory is None:
                raise ValueError("DIRECTORY_REQUIRED")
            result = run_fixture(args.directory, seed=args.seed, scenario=args.scenario)
            return result, 0 if result["verdict"] == "BOUNDED_SAFETY_VERIFIED" else 2
        from .evidence import load_json
        if args.path is None:
            raise ValueError("CERTIFICATE_PATH_REQUIRED")
        raw = load_json(args.path.read_bytes())
        keys = {"schema", "verdict", "engine_tree_sha", "connector_commit", "release_sha",
                "rulebook_sha", "oracle_sha", "scenario_sha", "trace_sha", "scope", "bounds"}
        return {"schema": "operator_judge_report_v1", "certificate": {k: v for k, v in raw.items() if k in keys},
                "certificate_trust": "UNVERIFIED_REQUIRES_INDEPENDENT_SIGNATURE",
                "trading_authorized": False}, 0
    if args.command == "paper":
        from .paper import ObservationJournal, collect
        if args.journal is None:
            raise ValueError("OBSERVATION_JOURNAL_REQUIRED")
        if args.action == "observe":
            if None in (args.research_root, args.research_python, args.observer_root):
                raise ValueError("PINNED_RESEARCH_AND_OBSERVER_REQUIRED")
            return collect(args.journal, research_root=args.research_root,
                           research_python=args.research_python, observer_root=args.observer_root,
                           interval=args.interval, iterations=args.iterations), 0
        return ObservationJournal.read(args.journal, now_ms), 0
    if args.command == "economics":
        from .research_bridge import economics
        if args.research_root is None or args.research_python is None:
            raise ValueError("PINNED_RESEARCH_RUNTIME_REQUIRED")
        result = economics(args.research_root, args.research_python)
        return result, 2 if result["verdict"] == "BLOCKED_ECONOMICS" else 0
    if args.command == "demo":
        from .evidence import load_json
        from .qualification import demo_report
        return demo_report(load_json(args.path.read_bytes()) if args.path else None, now_ms=now_ms), 2
    if args.command == "readiness":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        from .evidence import load_json
        from .operator import installed
        from .qualification import readiness
        spec = load_json(args.path.read_bytes()) if args.path else {}
        trust = {gate: {"key": Ed25519PublicKey.from_public_bytes(bytes.fromhex(value["public_key_hex"])),
                        "authority": value["authority"]} for gate, value in spec.get("trust", {}).items()}
        result = readiness(spec.get("evidence", {}), trust,
                           account_binding=spec.get("account_binding", "UNKNOWN"),
                           release_sha=spec.get("release_sha", "0" * 64),
                           engine_sha=spec.get("engine_sha", installed()["engine_tree_sha"]),
                           now_ms=now_ms, revoked_nonces=set(spec.get("revoked_nonces", [])),
                           minimum_authorization_ms=spec.get("minimum_authorization_ms", 0))
        return result, 2 if result["phase_c"] == "BLOCKED" else 0
    if args.command == "release" and args.action == "verify":
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        from .evidence import digest, load_json
        from .release import ReleaseFloor, ReleaseVerifier
        if args.path is None or args.floor is None:
            raise ValueError("PROTECTED_SETTINGS_AND_FLOOR_REQUIRED")
        settings = load_json(args.path.read_bytes())
        payload = settings["release"]["payload"]
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(settings["approver_public"]))
        with ReleaseFloor(args.floor) as floor:
            verified = ReleaseVerifier(key, "approver", payload["account_binding"],
                                       payload["risk_policy_sha"], payload["rulebook_sha"]).verify(
                                           settings["release"], now_ms=now_ms, floor=floor)
            floor.require_active(verified)
        return {"schema": "operator_release_v1", "verdict": "SIGNATURE_AND_ACTIVE_FLOOR_VERIFIED",
                "release_sha": verified.release_sha, "account_binding_sha": digest(payload["account_binding"]),
                "trust_source": "EXPLICIT_SETTINGS_FILE", "trading_authorized": False}, 0
    raise ValueError("COMMAND_NOT_SUPPORTED")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prop-production")
    parser.add_argument("--mode", default="SHADOW")
    parser.add_argument("command", choices=["status", "smoke", "portfolio", "release", "broker", "risk",
                                          "intent", "halt", "paper", "judge", "readiness", "demo",
                                          "economics", "fixture"])
    parser.add_argument("action", nargs="?", default="report")
    for name in ("journal", "path", "floor", "directory", "research-root", "research-python", "observer-root"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--now-ms", type=int)
    parser.add_argument("--intent-id")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--scenario", choices=["NORMAL", "ACCEPT_THEN_TIMEOUT", "OUT_OF_ENVELOPE"], default="NORMAL")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args(argv)
    if args.mode != "SHADOW":
        print(json.dumps({"error": "MODE_NOT_PERMITTED", "trading_authorized": False}))
        return 2
    try:
        result, code = _command(args)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        result, code = {"schema": "operator_error_v1", "state": "BLOCKED",
                        "error": "EVIDENCE_STORAGE_OR_PERMISSION", "trading_authorized": False}, 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return code
