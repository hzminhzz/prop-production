"""Read-only operator projection. Account, source and broker identities are redacted."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .evidence import digest
from .health import assess_health
from .journal import Journal


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


def read_model(path: Path, command: str, now_ms: int, intent_id: str | None = None) -> dict[str, Any]:
    state = Journal.read(path)
    runtime = state["runtime"]
    result: dict[str, Any] = {
        "schema": "operator_" + command + "_v1", "trading_authorized": False,
        "account_binding_sha": digest(state["account_binding"]), "journal_sequence": state["sequence"],
        "release_sha": state.get("active_release_sha"), "halts": state["halts"],
    }
    if command == "status":
        result.update(state="HALTED" if state["halts"] else runtime["state"],
                      account_readiness=runtime["certainty"], mode="SHADOW", order_route="NONE",
                      health=assess_health(state, now_ms=now_ms), installed=installed(),
                      scientifically_qualified=False)
    elif command == "portfolio":
        result["decisions"] = [{"decision_id": d["receipt"]["decision_id"],
            "release_sha": d["receipt"]["release_sha"], "status": d["receipt"]["status"],
            "reasons": d["receipt"]["reasons"],
            "pods": [{"pod_id": p["pod_id"], "allocation": p.get("allocation"),
                      "requested_notionals": p["requested_notionals"]}
                     for p in d["receipt"]["requested_pods"]]}
            for d in state["decisions"].values()]
    elif command == "broker":
        result.update(broker_epoch=runtime["broker_epoch"], certainty=runtime["certainty"],
                      last_snapshot_ms=runtime.get("last_snapshot_ms"),
                      provider_cutoff_sha=digest(runtime.get("provider_cutoff")),
                      flat_confirmed=assess_health(state, now_ms=now_ms)["flat_confirmed"])
    elif command == "risk":
        result.update(finance=state["finance"], reservations=state["reservations"],
                      pod_loss_used=state.get("pod_loss_used", {}))
    elif command == "intent":
        keep = {"intent_id", "decision_id", "release_sha", "market", "quantity", "filled", "status",
                "reduce_only", "broker_epoch", "boot_epoch", "created_at_ms", "expires_at_ms",
                "proof_sha", "reservation_id"}
        result["intents"] = [{k: v for k, v in value.items() if k in keep}
                             for key, value in state["intents"].items() if intent_id is None or key == intent_id]
    return result
