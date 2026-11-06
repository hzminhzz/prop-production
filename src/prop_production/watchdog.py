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
FINANCIAL_STALE_MS = 120_000    # journal progress older than this is an incident
MIN_DISK_FREE_BYTES = 512 * 1024 * 1024


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _read_last_assessment(path: Path) -> dict[str, Any] | None:
    """Most recent persisted assessment, for sequence-advancement tracking."""
    try:
        lines = path.read_text().strip().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def assess(state_root: Path, *, now_ms: int) -> dict[str, Any]:
    """One fail-closed assessment of the deployed runtime. Read-only."""
    reasons: list[str] = []
    guardian = _read_json(state_root / "health" / "guardian" / "heartbeat.json")
    observer = _read_json(state_root / "health" / "observer" / "heartbeat.json")
    bybit = _read_json(state_root / "health" / "bybit" / "heartbeat.json")

    guardian_ms = guardian.get("heartbeat_ms") if guardian else None
    if not isinstance(guardian_ms, int) or not 0 <= now_ms - guardian_ms <= STALE_GUARDIAN_MS:
        reasons.append("GUARDIAN_STALE_OR_UNKNOWN")
    observer_ms = observer.get("heartbeat_ms") if observer else None
    if not isinstance(observer_ms, int) or not 0 <= now_ms - observer_ms <= STALE_OBSERVER_MS:
        reasons.append("OBSERVER_STALE_OR_UNKNOWN")
    if guardian and guardian.get("halts"):
        reasons.extend(f"HALT:{halt}" for halt in guardian["halts"])
    # Financial progress: a fresh heartbeat with no journal sequence or no
    # reconciled certainty is an incident, not progress.
    progress = guardian.get("last_progress_ms") if guardian else None
    sequence = guardian.get("journal_sequence") if guardian else None
    if reasons == [] and (not isinstance(progress, int) or now_ms - progress > FINANCIAL_STALE_MS):
        reasons.append("FINANCIAL_PROGRESS_STALE")
    if reasons == [] and (not isinstance(sequence, int) or sequence < 1):
        reasons.append("FINANCIAL_PROGRESS_STALE")
    certainty = guardian.get("certainty") if guardian else None
    if reasons == [] and certainty != "COHERENT":
        reasons.append("RECONCILIATION_UNCERTAIN")
    # Progress contract: the driver reconciles on a cadence shorter than the
    # watchdog window, so the journal sequence must advance across consecutive
    # assessments. Unchanged sequence across two windows (same boot epoch) is a
    # hung worker, not legitimate quiet state — the deployed cadence is 4s.
    previous = _read_last_assessment(state_root / "watchdog" / "assessments.jsonl")
    boot_epoch = guardian.get("boot_epoch") if guardian else None
    if (reasons == [] and previous is not None
            and previous.get("guardian_journal_sequence") == sequence
            and previous.get("guardian_boot_epoch") == boot_epoch
            and isinstance(previous.get("assessed_at_ms"), int)
            and 0 <= now_ms - previous["assessed_at_ms"] <= 2 * WATCH_INTERVAL_S * 1000):
        reasons.append("SEQUENCE_NOT_ADVANCING")
    if observer and observer.get("healthy") is False:
        reasons.append("OBSERVER_UNHEALTHY")
    bybit_ms = bybit.get("heartbeat_ms") if bybit else None
    if not isinstance(bybit_ms, int) or not 0 <= now_ms - bybit_ms <= STALE_OBSERVER_MS:
        bybit_status = "UNKNOWN_OR_STALE"
        reasons.append("BYBIT_DEMO_OBSERVER_STALE_OR_UNKNOWN")
    elif (bybit is not None and bybit.get("healthy") is True
          and bybit.get("identity_verified") is True
          and bybit.get("opening_ready") is False and bybit.get("mutations") == 0):
        bybit_status = "OBSERVED_READ_ONLY"
    else:
        bybit_status = "BLOCKED"
        reasons.append("BYBIT_DEMO_EVIDENCE_UNHEALTHY")

    # Market/source: no connected market source exists in this deployment —
    # reported explicitly, never folded into HEALTHY (§6: missing evidence
    # for a required dimension is a blocked state, not a healthy one).
    market_status = "UNKNOWN"  # no market source is currently connected
    reasons.append("MARKET_SOURCE_UNKNOWN")

    disk = shutil.disk_usage(state_root)
    free = disk.free
    if free < MIN_DISK_FREE_BYTES:
        reasons.append("DISK_EXHAUSTION_RISK")

    # Overall status: HEALTHY requires every required dimension evidenced.
    # Missing market/source evidence keeps the runtime OPERATIONAL_BUT_BLOCKED.
    if any(r.startswith("HALT:") for r in reasons):
        status = "HALTED"
    elif reasons:
        status = "OPERATIONAL_BUT_BLOCKED"
    else:
        status = "HEALTHY"

    return {"schema": "watchdog_assessment_v2", "assessed_at_ms": now_ms,
            "status": status, "reasons": sorted(set(reasons)),
            "market_status": market_status, "bybit_status": bybit_status,
            "bybit_observer_heartbeat_ms": bybit_ms,
            "guardian_heartbeat_ms": guardian_ms, "observer_heartbeat_ms": observer_ms,
            "guardian_last_progress_ms": guardian.get("last_progress_ms") if guardian else None,
            "guardian_journal_sequence": guardian.get("journal_sequence") if guardian else None,
            "guardian_boot_epoch": boot_epoch,
            "guardian_certainty": guardian.get("certainty") if guardian else None,
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
