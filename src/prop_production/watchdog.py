"""Independent watchdog: observes deployed Guardian SHADOW services fail-closed.

The watchdog never authorizes trading. It writes durable evidence about
process liveness, evidence freshness and disk headroom. A stale or absent
watchdog heartbeat is itself a blocking reason for every consumer, so a
failed watchdog can never silently authorize anything.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from .evidence import canonical

WATCH_INTERVAL_S = 5  # watchdog heartbeat must stay under assess_health's 10s threshold
STALE_GUARDIAN_MS = 120_000     # guardian progress older than this is an incident
STALE_OBSERVER_MS = 120_000     # observer heartbeat older than this is an incident
MIN_DISK_FREE_BYTES = 512 * 1024 * 1024


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def assess(state_root: Path, *, now_ms: int) -> dict[str, Any]:
    """One fail-closed assessment of the deployed runtime. Read-only."""
    reasons: list[str] = []
    guardian = _read_json(state_root / "health" / "guardian.json")
    observer = _read_json(state_root / "health" / "observer.json")

    guardian_ms = guardian.get("heartbeat_ms") if guardian else None
    if not isinstance(guardian_ms, int) or not 0 <= now_ms - guardian_ms <= STALE_GUARDIAN_MS:
        reasons.append("GUARDIAN_STALE_OR_UNKNOWN")
    observer_ms = observer.get("heartbeat_ms") if observer else None
    if not isinstance(observer_ms, int) or not 0 <= now_ms - observer_ms <= STALE_OBSERVER_MS:
        reasons.append("OBSERVER_STALE_OR_UNKNOWN")
    if guardian and guardian.get("halts"):
        reasons.extend(f"HALT:{halt}" for halt in guardian["halts"])
    if observer and observer.get("healthy") is False:
        reasons.append("OBSERVER_UNHEALTHY")

    disk = shutil.disk_usage(state_root)
    free = disk.free
    if free < MIN_DISK_FREE_BYTES:
        reasons.append("DISK_EXHAUSTION_RISK")

    return {"schema": "watchdog_assessment_v1", "assessed_at_ms": now_ms,
            "status": "BLOCKED" if reasons else "HEALTHY", "reasons": sorted(set(reasons)),
            "guardian_heartbeat_ms": guardian_ms, "observer_heartbeat_ms": observer_ms,
            "disk_free_bytes": free, "trading_authorized": False}


def write_evidence(state_root: Path, assessment: dict[str, Any]) -> None:
    """Append-only watchdog evidence plus an atomically replaced heartbeat."""
    state_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    with open(state_root / "assessments.jsonl", "ab") as sink:
        sink.write(canonical(assessment) + b"\n")
    heartbeat = state_root / "heartbeat.json"
    temporary = heartbeat.with_suffix(".tmp")
    temporary.write_bytes(canonical({"schema": "watchdog_heartbeat_v1",
                                     "heartbeat_ms": assessment["assessed_at_ms"]}))
    os.replace(temporary, heartbeat)


def watch(evidence_dir: Path, *, state_root: Path, interval_s: int = WATCH_INTERVAL_S) -> None:
    running = True

    def stop(*_: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        write_evidence(evidence_dir, assess(state_root, now_ms=int(time.time() * 1000)))
        for _ in range(interval_s * 10):
            if not running:
                return
            time.sleep(0.1)


def main() -> None:
    state_root = Path(os.environ.get("PROP_STATE_ROOT", "/var/lib/prop-production"))
    watch(state_root / "watchdog", state_root=state_root)


if __name__ == "__main__":
    main()
