"""Bounded read-only operator projection.

Account, source, and broker identities are redacted. The model may be exported by
a separately authorized owner process, never pulled by a browser or Marimo.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import __version__
from .evidence import digest
from .health import assess_health
from .journal import Journal

WALLET_ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}\b")
PRIVATE_KEYS = frozenset({
    "account_binding", "source_identity", "wallet_address", "private_key",
    "secret", "endpoint", "api_key", "signed_source_envelope",
})


def _scrub(value: Any, account: str) -> Any:
    """No raw source/broker/account identifiers in restricted consumer files."""
    if isinstance(value, str):
        cleaned = WALLET_ADDRESS.sub("[PRIVATE_WALLET]", value)
        return cleaned.replace(account, "[PRIVATE_ACCOUNT]") if account else cleaned
    if isinstance(value, dict):
        return {
            k: _scrub(v, account)
            for k, v in value.items() if k not in PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_scrub(v, account) for v in value]
    return value


def installed() -> dict[str, Any]:
    package = Path(__file__).parent
    files = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(package.rglob("*.py"))}
    root = package.parents[1]
    commit = "UNKNOWN"
    dirty: bool | None = None
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True, timeout=2).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                   check=True, capture_output=True, text=True, timeout=2).stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return {"version": __version__, "git_commit": commit, "worktree_dirty": dirty,
            "engine_tree_sha": digest(files), "source_files": files}


def _health_evidence(journal_path: Path) -> dict[str, Any]:
    """Watchdog heartbeat evidence for the health assessment (absent = stale)."""
    try:
        value = json.loads((journal_path.parent.parent / "watchdog" / "heartbeat.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _exposure(state: dict[str, Any]) -> dict[str, Any]:
    """Diagnostic mark exposure only when the broker-owned reconciliation is coherent."""
    runtime = state.get("runtime", {})
    if (runtime.get("certainty") != "COHERENT"
            or type(runtime.get("last_snapshot_ms")) is not int):
        return {"status": "UNKNOWN", "gross": None, "net": None,
                "observed_at_ms": None}
    positions = state.get("positions")
    marks = state.get("last_marks")
    if not isinstance(positions, dict) or not isinstance(marks, dict):
        return {"status": "UNKNOWN", "gross": None, "net": None,
                "observed_at_ms": runtime.get("last_snapshot_ms")}
    gross, net = Decimal("0"), Decimal("0")
    if len(positions) > 200:
        return {"status": "BLOCKED", "gross": None, "net": None,
                "observed_at_ms": runtime.get("last_snapshot_ms")}
    for market, raw_quantity in positions.items():
        qty, mark = _decimal(raw_quantity), _decimal(marks.get(market))
        if qty is None or (qty and (mark is None or mark <= 0)):
            return {"status": "UNKNOWN", "gross": None, "net": None,
                    "observed_at_ms": runtime.get("last_snapshot_ms")}
        if not qty:
            continue
        notional = qty * mark
        gross += abs(notional)
        net += notional
    return {"status": "VERIFIED_OWNER_MARK_SNAPSHOT", "gross": str(gross),
            "net": str(net), "observed_at_ms": runtime["last_snapshot_ms"]}


def _source_clocks(proposal: object) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        return {"first_known_ms": None, "due_ms": None, "batch_decision_ms": None,
                "source_receipts": [], "source_receipts_truncated": False}
    receipts = proposal.get("source_receipts")
    receipts = receipts if isinstance(receipts, list) else []
    visible = []
    for receipt in receipts[:12]:
        if not isinstance(receipt, dict):
            continue
        envelope = receipt.get("envelope")
        body = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(body, dict):
            continue
        visible.append({
            # Never export raw wallet/source identity or signed source payloads.
            "receipt_sha": digest(receipt),
            "source_event_ms": body.get("source_event_ms")
            if type(body.get("source_event_ms")) is int else None,
            "source_known_ms": body.get("known_at_ms")
            if type(body.get("known_at_ms")) is int else None,
            "local_known_ms": receipt.get("local_known_ms")
            if type(receipt.get("local_known_ms")) is int else None,
            "source_sequence": body.get("sequence")
            if type(body.get("sequence")) is int else None,
        })
    return {
        "first_known_ms": proposal.get("first_known_ms")
        if type(proposal.get("first_known_ms")) is int else None,
        "due_ms": proposal.get("due_ms")
        if type(proposal.get("due_ms")) is int else None,
        "batch_decision_ms": proposal.get("decision_ms")
        if type(proposal.get("decision_ms")) is int else None,
        "source_receipts": visible,
        "source_receipts_truncated": len(receipts) > 12,
    }


def project_state(state: dict[str, Any], command: str, now_ms: int,
                  journal_path: Path | None = None,
                  intent_id: str | None = None) -> dict[str, Any]:
    """A single already-verified Guardian snapshot, never mutated."""
    if command not in {"status", "portfolio", "risk", "broker", "intent"}:
        raise ValueError("UNSUPPORTED_READ_PROJECTION")
    runtime = state["runtime"]
    result: dict[str, Any] = {
        "schema": "operator_" + command + "_v1", "trading_authorized": False,
        "account_binding_sha": digest(state["account_binding"]),
        "journal_sequence": state["sequence"],
        "release_sha": state.get("active_release_sha"), "halts": state["halts"],
    }
    if command == "status":
        health = assess_health(state, now_ms=now_ms, watchdog_ms=(
            _health_evidence(journal_path).get("heartbeat_ms")
            if journal_path is not None else None
        ))
        result.update(state="HALTED" if state["halts"] else runtime["state"],
                      account_readiness=runtime["certainty"], mode="SHADOW", order_route="NONE",
                      health=health, installed=installed(), scientifically_qualified=False)
    elif command == "portfolio":
        records = list(state["decisions"].values())
        result["decisions"] = [
            {
                "decision_id": d["receipt"]["decision_id"],
                "release_sha": d["receipt"]["release_sha"],
                "status": d["receipt"]["status"], "reasons": d["receipt"]["reasons"],
                "pods": [
                    {"pod_id": p["pod_id"], "allocation": p.get("allocation"),
                     "requested_notionals": p["requested_notionals"]}
                    for p in d["receipt"]["requested_pods"][:20]
                ],
                "proposal_id": d["receipt"].get("proposal_id"),
                "guardian_decision_at_ms": d["receipt"].get("known_at_ms"),
                "decision_sequence": d.get("decision_sequence"),
                **_source_clocks(d.get("proposal")),
            } for d in records[-40:]
        ]
        result["total_decisions"] = len(records)
        result["truncated"] = len(records) > 40
    elif command == "broker":
        result.update(broker_epoch=runtime["broker_epoch"], certainty=runtime["certainty"],
                      last_snapshot_ms=runtime.get("last_snapshot_ms"),
                      provider_cutoff_sha=digest(runtime.get("provider_cutoff")),
                      flat_confirmed=assess_health(state, now_ms=now_ms)["flat_confirmed"])
        observed = list(state["fills"].values())
        result["fills"] = [
            {"execution_id_sha": digest(f["execution_id"]),
             "intent_id": f["intent_id"], "market": f["market"],
             "quantity": f["quantity"], "price": f["price"], "fee": f["fee"],
             "known_at_ms": f["known_at_ms"]}
            for f in observed[-120:]
        ]
        result["total_fills"] = len(observed)
        result["fills_truncated"] = len(observed) > 120
    elif command == "risk":
        result.update(finance=state["finance"],
                      reservations=state["reservations"],
                      pod_loss_used=state.get("pod_loss_used", {}),
                      exposure=_exposure(state))
    else:
        keep = {"intent_id", "decision_id", "release_sha", "market", "quantity", "filled", "status",
                "reduce_only", "broker_epoch", "boot_epoch", "created_at_ms", "expires_at_ms",
                "proof_sha", "reservation_id", "sent_at_ms"}
        all_intents = list(state["intents"].values())
        selected = (
            [value for value in all_intents if value.get("intent_id") == intent_id]
            if intent_id is not None else all_intents[-120:]
        )
        result["intents"] = [{k: v for k, v in value.items() if k in keep}
                             for value in selected]
        result["total_intents"] = len(all_intents)
        result["truncated"] = intent_id is None and len(all_intents) > 120
    return _scrub(result, state["account_binding"])


def read_model(path: Path, command: str, now_ms: int, intent_id: str | None = None) -> dict[str, Any]:
    state = Journal.read(path)
    return project_state(state, command, now_ms, path, intent_id)
