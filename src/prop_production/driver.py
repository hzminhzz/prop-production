"""Persistent supervisor for deployed stdin-driven SHADOW workers.

Owns one worker subprocess, feeds it the periodic command for its role and
logs every response. All reads are byte-framed with hard deadlines and size
caps: a hung or silent child can never block the supervisor indefinitely.
Crash recovery is delegated to systemd Restart=always; the worker's own
journal remains the durable state.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PERIODIC: dict[str, dict[str, Any] | None] = {"guardian": {"command": "reconcile"},
                                              "planner": {"command": "propose", "broker_epoch": 0,
                                                          "quotes": {}},
                                              "broker": None}
INTERVAL_S = 4        # default cadence; overridable via PROP_DRIVER_INTERVAL
RESPONSE_TIMEOUT_S = 30
STARTUP_TIMEOUT_S = 30
MAX_RESPONSE_BYTES = 1_000_000


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    """Read a bounded integer setting; invalid values abort startup."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit(f"{name}_INVALID: {raw!r}") from error
    if not low <= value <= high:
        # Bounds protect the watchdog (10s heartbeat threshold) and the
        # financial-progress freshness contract; do not weaken them silently.
        raise SystemExit(f"{name}_OUT_OF_BOUNDS: {value} not in [{low}, {high}]")
    return value


class BoundedReader:
    """Byte-frame line reader: hard deadline, size cap, read-ahead buffer."""

    def __init__(self, fd: int, *, max_bytes: int = MAX_RESPONSE_BYTES):
        self._fd = fd
        self._buffer = b""
        self._max_bytes = max_bytes

    def readline(self, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        while True:
            if b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                return line
            if len(self._buffer) > self._max_bytes:
                raise ValueError("WORKER_RESPONSE_OVERSIZED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"WORKER_RESPONSE_TIMEOUT after {timeout_s}s")
            ready, _, _ = select.select([self._fd], [], [], min(0.1, remaining))
            if not ready:
                continue
            chunk = os.read(self._fd, 65536)
            if chunk == b"":
                raise EOFError("WORKER_STDOUT_CLOSED")
            self._buffer += chunk


def _now_ms() -> int:
    return int(time.time() * 1000)


def _heartbeat(root: Path, worker: subprocess.Popen[bytes], reader: BoundedReader) -> None:
    """Write guardian heartbeat evidence for the independent watchdog."""
    assert worker.stdin is not None
    worker.stdin.write((json.dumps({"command": "status", "now_ms": _now_ms()}) + "\n").encode())
    worker.stdin.flush()
    response = reader.readline(RESPONSE_TIMEOUT_S)
    try:
        state = json.loads(response.decode()) if response.strip() else {}
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


def drive(role: str, root: Path, *, interval_s: int) -> None:
    worker = subprocess.Popen(
        [sys.executable, "-m", "prop_production.worker", role, str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
    assert worker.stdout is not None and worker.stdin is not None
    reader = BoundedReader(worker.stdout.fileno())
    try:
        ready = reader.readline(STARTUP_TIMEOUT_S)  # startup hang is bounded
    except (TimeoutError, EOFError, ValueError) as error:
        print(json.dumps({"schema": "driver_failure_v1", "role": role,
                          "stage": "startup", "error": type(error).__name__}), flush=True)
        worker.kill()
        worker.wait()
        return
    print(ready.decode(errors="replace"), flush=True)
    if role == "guardian":
        deadline = time.time() + 30
        while not (root / "broker" / "b.sock").exists():
            if time.time() > deadline or worker.poll() is not None:
                return  # Restart=always retries; a latched halt stays durable
            time.sleep(0.5)
    try:
        while worker.poll() is None:
            template = PERIODIC[role]
            assert template is not None
            command = {"now_ms": _now_ms(), **template}
            worker.stdin.write((json.dumps(command) + "\n").encode())
            worker.stdin.flush()
            try:
                response = reader.readline(RESPONSE_TIMEOUT_S)
            except (TimeoutError, EOFError, ValueError) as error:
                print(json.dumps({"schema": "driver_failure_v1", "role": role,
                                  "stage": "command", "error": type(error).__name__,
                                  "at_ms": _now_ms()}), flush=True)
                break  # hung or dead worker; Restart=always handles recovery
            print(json.dumps({"schema": "driver_response_v1", "role": role,
                              "command": command["command"], "at_ms": _now_ms(),
                              "response": response.decode(errors="replace").strip() or None}),
                  flush=True)
            if role == "guardian":
                _heartbeat(root, worker, reader)
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
                worker.wait()


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    interval = _bounded_int("PROP_DRIVER_INTERVAL", INTERVAL_S, 1, 60)
    global RESPONSE_TIMEOUT_S
    RESPONSE_TIMEOUT_S = _bounded_int("PROP_DRIVER_RESPONSE_TIMEOUT", RESPONSE_TIMEOUT_S, 5, 300)
    if len(sys.argv) != 3 or sys.argv[1] not in PERIODIC or PERIODIC[sys.argv[1]] is None:
        raise SystemExit("usage: driver.py {guardian|planner} STATE_ROOT")
    drive(sys.argv[1], Path(sys.argv[2]).resolve(), interval_s=interval)


if __name__ == "__main__":
    main()
