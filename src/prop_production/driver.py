"""Persistent supervisor for deployed stdin-driven SHADOW workers.

Owns one worker subprocess, feeds it the periodic command for its role and
logs every response. Crash recovery is delegated to systemd Restart=always;
the worker's own journal remains the durable state.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

PERIODIC: dict[str, dict[str, Any] | None] = {"guardian": {"command": "reconcile"},
                                              "planner": {"command": "propose", "broker_epoch": 0,
                                                          "quotes": {}},
                                              "broker": None}
RESPONSE_TIMEOUT_S = 30  # hard bound per worker response
INTERVAL_S = 4  # keep reconcile ages under assess_health's 5s/10s staleness thresholds


def _now_ms() -> int:
    return int(time.time() * 1000)


def _readline_bounded(stream: IO[str], timeout_s: float) -> str:
    """Read one line with a hard time bound; a hung child cannot stall the loop."""
    import select

    deadline = time.time() + timeout_s
    pieces: list[str] = []
    while time.time() < deadline:
        if not select.select([stream], [], [], 0.1)[0]:
            continue
        piece = stream.readline()
        if piece == "":
            raise EOFError("WORKER_STDOUT_CLOSED")
        pieces.append(piece)
        if piece.endswith("\n"):
            return "".join(pieces)
    raise TimeoutError(f"WORKER_RESPONSE_TIMEOUT after {timeout_s}s")


def _heartbeat(root: Path, worker: subprocess.Popen[str]) -> None:
    """Write guardian heartbeat evidence for the independent watchdog."""
    assert worker.stdin is not None and worker.stdout is not None
    worker.stdin.write(json.dumps({"command": "status", "now_ms": _now_ms()}) + "\n")
    worker.stdin.flush()
    response = worker.stdout.readline()
    try:
        state = json.loads(response) if response.strip() else {}
    except ValueError:
        state = {}
    halts = state.get("halts") if isinstance(state, dict) else None
    runtime = state.get("runtime", {}) if isinstance(state, dict) else {}
    # Role-owned heartbeat: only prop-runtime writes here (0750 dir, group reads).
    health = root / "health" / "guardian"
    health.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = health / "heartbeat.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "schema": "guardian_heartbeat_v2", "heartbeat_ms": _now_ms(),
        "journal_sequence": state.get("sequence") if isinstance(state, dict) else None,
        "last_progress_ms": runtime.get("last_progress_ms"),
        "certainty": runtime.get("certainty"), "runtime_state": runtime.get("state"),
        "boot_epoch": runtime.get("boot_epoch"), "broker_epoch": runtime.get("broker_epoch"),
        "halts": halts or [],  # a missing halts key is an empty list, never an implicit pass
    }))
    os.replace(temporary, target)
    os.chmod(target, 0o640)  # watchdog (group prop) must read this evidence


def drive(role: str, root: Path, *, interval_s: int = INTERVAL_S) -> None:
    worker = subprocess.Popen(
        [sys.executable, "-m", "prop_production.worker", role, str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
    assert worker.stdout is not None
    ready = worker.stdout.readline()
    print(ready, flush=True)  # forward the worker's ready line as the service ready line
    if role == "guardian":
        deadline = time.time() + 30
        while not (root / "broker" / "b.sock").exists():
            if time.time() > deadline or worker.poll() is not None:
                return  # systemd Restart=always retries; a latched halt stays durable
            time.sleep(0.5)
    try:
        while worker.poll() is None:
            assert worker.stdin is not None and worker.stdout is not None
            template = PERIODIC[role]
            assert template is not None
            command = {"now_ms": _now_ms(), **template}
            worker.stdin.write(json.dumps(command) + "\n")
            worker.stdin.flush()
            try:
                response = _readline_bounded(worker.stdout, RESPONSE_TIMEOUT_S)
            except (TimeoutError, EOFError) as error:
                print(json.dumps({"schema": "driver_failure_v1", "role": role,
                                  "error": type(error).__name__, "at_ms": _now_ms()}), flush=True)
                break  # hung or dead worker; Restart=always handles recovery
            print(json.dumps({"schema": "driver_response_v1", "role": role,
                              "command": command["command"], "at_ms": _now_ms(),
                              "response": response.strip() or None}), flush=True)
            if role == "guardian":
                _heartbeat(root, worker)
            for _ in range(interval_s * 10):
                if worker.poll() is not None:
                    break
                time.sleep(0.1)
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in PERIODIC or PERIODIC[sys.argv[1]] is None:
        raise SystemExit("usage: driver.py {guardian|planner} STATE_ROOT")
    drive(sys.argv[1], Path(sys.argv[2]).resolve())


if __name__ == "__main__":
    main()
