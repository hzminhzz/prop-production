"""Owner-only bounded capture writer for the unprivileged Marimo dashboard.

Usage (only after separate protected installation authorization):
    python -m prop_production.dashboard_capture --journal /protected/account.sqlite \
        --output-dir /owner-managed/restricted-captures

The dashboard NEVER calls this module, CLI, or a financial journal. The
pre-created output directory must be owner-private, no symlinks, no group/world
writes; group read access is a separately reviewed OS permissions decision.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .journal import Journal
from .operator import project_state

COMMANDS = ("status", "risk", "broker", "portfolio", "intent", "history")
MAX_CAPTURE_BYTES = 131_072


def _utc_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


def build_captures(
    state: dict[str, Any], history: list[dict[str, Any]], *,
    now_ms: int,
) -> dict[str, bytes]:
    """One verified journal generation for every capture, no source extrapolation."""
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("INVALID_CAPTURE_CLOCK")
    runtime = state["runtime"]
    progress_ms = runtime.get("last_progress_ms")
    valid_progress = type(progress_ms) is int and 0 <= progress_ms <= now_ms
    progress = progress_ms if valid_progress else 0
    finance_ms = state.get("finance", {}).get("observed_at_ms")
    broker_ms = runtime.get("last_snapshot_ms")
    timestamps = {
        "status": progress,
        "risk": finance_ms if type(finance_ms) is int and 0 <= finance_ms <= now_ms else 0,
        "broker": broker_ms if type(broker_ms) is int and 0 <= broker_ms <= now_ms else 0,
        "portfolio": progress,
        "intent": progress,
        "history": progress,
    }
    native = {cmd: project_state(state, cmd, now_ms) for cmd in COMMANDS[:-1]}
    # Historical exact points are reconstructed only from integrity-checked
    # Guardian RECONCILE snapshots. No interpolation or broker promotion.
    rows = []
    for item in history[-256:]:
        if not isinstance(item, dict):
            continue
        rows.append({
            key: item.get(key) for key in (
                "journal_sequence", "reconciled_at_ms", "observed_at_ms",
                "release_sha", "equity", "unrealized", "cash", "risk_floor",
                "daily_floor", "internal_floor", "trailing_floor",
                "headroom", "day", "realized_made",
            )
        })
    native["history"] = {
        "schema": "operator_history_v1", "trading_authorized": False,
        "account_binding_sha": native["risk"]["account_binding_sha"],
        "journal_sequence": state["sequence"],
        "release_sha": state.get("active_release_sha"),
        "evidence_class": "PAPER_SHADOW",
        "source_kind": "GUARDIAN_VERIFIED_RECONCILE_SNAPSHOTS",
        "source_coverage": "RECENT_BOUNDED_TAIL_NOT_COMPLETE_HISTORY",
        "rows": rows, "rows_truncated_to": 256,
        "scientifically_qualified": False,
    }
    output = {}
    for cmd in COMMANDS:
        payload = {
            "schema": "dashboard_guardian_capture_v1", "command": cmd,
            "source_observed_at": _utc_ms(timestamps[cmd]),
            "captured_at": _utc_ms(now_ms),
            "native": native[cmd],
        }
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        if len(serialized) > MAX_CAPTURE_BYTES:
            raise ValueError("DASHBOARD_CAPTURE_BYTE_BUDGET_EXCEEDED")
        output[cmd] = serialized
    return output


def export_capture(journal: Path, output_dir: Path, *, now_ms: int) -> dict[str, Any]:
    """Explicit owner command. No dashboard-controlled inputs or journal mutation."""
    state, recent_finance = Journal.read_with_finance_history(journal, limit=256)
    objects = build_captures(state, recent_finance, now_ms=now_ms)
    # Root must exist and be owned by the producer identity. No implicit mkdir
    # or permissions changes and no ability to follow an output-path symlink.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(output_dir, flags)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError("UNTRUSTED_CAPTURE_OUTPUT_DIRECTORY")
        for name, payload in objects.items():
            temp = f".{name}.{os.getpid()}.new"
            subfd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o640, dir_fd=fd)
            try:
                # Explicit mode despite a producer service's restrictive umask;
                # directory group membership is a separate approved OS boundary.
                os.fchmod(subfd, 0o640)
                with os.fdopen(subfd, "wb", closefd=False) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(subfd)
                os.replace(temp, f"{name}.json", src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                os.close(subfd)
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
        os.fsync(fd)
    finally:
        os.close(fd)
    return {
        "schema": "dashboard_capture_export_receipt_v1",
        "status": "FILES_WRITTEN",
        "capture_count": len(objects),
        "journal_sequence": state["sequence"],
        "trading_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_capture(args.journal, args.output_dir,
                                    now_ms=time.time_ns() // 1_000_000), sort_keys=True))


if __name__ == "__main__":
    main()
