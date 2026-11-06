"""Pinned, credential-clean subprocess access to research-owned read-only capabilities."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .evidence import load_json

RESEARCH_COMMIT = "fcf79b8e4d8012b46cdf8d2ad190af06a6e8dd5e"


def verify_checkout(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("RESEARCH_CHECKOUT")
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    ).stdout.strip()
    # A pinned HEAD alone is not sufficient: untracked importable modules and
    # staged edits also change the subprocess' effective executable code.
    dirty = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "src",
            "pyproject.toml",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    ).stdout
    if revision != RESEARCH_COMMIT or dirty.strip():
        raise ValueError("RESEARCH_REVISION_NOT_PINNED")


def _invoke(root: Path, python: Path, arguments: list[str]) -> dict[str, Any]:
    verify_checkout(root)
    result = subprocess.run(
        [str(python), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
        close_fds=True,
        env={"PATH": os.defpath, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode not in {0, 2} or len(result.stdout) > 1000000:
        raise ValueError("RESEARCH_READ_FAILED")
    return dict(load_json(result.stdout))


def economics(root: Path, python: Path, tapes: dict[str, Path] | None = None) -> dict[str, Any]:
    arguments = ["-m", "copytrade_pm.production_economics"]
    for policy, path in (tapes or {}).items():
        if policy not in {"fixed", "volatility-throttled", "barrier-aware"}:
            raise ValueError("ECONOMIC_POLICY")
        arguments += ["--" + policy, str(path)]
    # Economics output may contain finite numeric statistics; it is not a financial authority input.
    import json

    verify_checkout(root)
    result = subprocess.run(
        [str(python), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        close_fds=True,
        env={"PATH": os.defpath, "PYTHONPATH": str(root / "src")},
    )
    if result.returncode not in {0, 2}:
        raise ValueError("NATIVE_ECONOMICS_UNAVAILABLE")
    return dict(
        json.loads(
            result.stdout, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE"))
        )
    )


def observe(root: Path, python: Path, observer_root: Path, now_iso: str) -> dict[str, Any]:
    # The owning repository verifies existing receipt chains and OS heartbeat identity.
    # Only hashes, counts and reason codes cross into this application.
    script = """
import json, sys
from pathlib import Path
from copytrade_pm.pm_forward_observer_health import inspect_public_observer_heartbeat, _readonly_tape_snapshot
from copytrade_pm.pm03_account import digest
root, now = Path(sys.argv[1]), sys.argv[2]
try:
    health = inspect_public_observer_heartbeat(root, journal_db=root / 'paper-events.sqlite3', as_of=now)
    snapshot = _readonly_tape_snapshot(root / 'paper-events.sqlite3')
    reasons = sorted({str(code).split(':', 1)[0] for code in health['reason_codes']})
    alive = health.get('observer_process_verified') is True and not any(
        'STALE' in code or 'FAILURE' in code for code in reasons)
    result = {'schema': 'read_only_source_observation_v1', 'native_tape_sha': digest(snapshot),
        'source_receipts': sum(x['cursor'] for x in snapshot['source'].values()),
        'market_receipts': sum(x['cursor'] for x in snapshot['quotes'].values()),
        'collateral_receipts': sum(x['cursor'] for x in snapshot['collateral'].values()),
        'native_observer_health': health['health'], 'upstream_alive': alive,
        'source_nav_attested': False, 'trade_volume_complete': False,
        'blockers': reasons + ['APPROVED_PRODUCTION_RELEASE_MISSING', 'SOURCE_SCOPE_AND_FOLLOWER_EXECUTION_UNVERIFIED']}
except (OSError, ValueError, KeyError, TypeError):
    result = {'schema': 'read_only_source_observation_v1', 'native_tape_sha': '0'*64,
        'source_receipts': 0, 'market_receipts': 0, 'collateral_receipts': 0,
        'native_observer_health': 'UNKNOWN', 'upstream_alive': False,
        'source_nav_attested': False, 'trade_volume_complete': False,
        'blockers': ['NATIVE_EVIDENCE_UNAVAILABLE']}
print(json.dumps(result, sort_keys=True))
"""
    return _invoke(root, python, ["-c", script, str(observer_root), now_iso])
