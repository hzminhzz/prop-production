"""Immutable release receipts and reversible activation; never restore financial state."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

UNITS = tuple("prop-production-" + role + ".service" for role in (
    "broker", "guardian", "planner", "observer", "watchdog", "bybit-observer"
))
MARKERS = ("COMMIT", "TREE", "SOURCE.tar", "SOURCE.sha256", "compatibility.json")
ARTIFACT_DIRS = ("source", "connector", "units", "dist", "venv")
ServiceCall = Callable[[list[str]], None]


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _inventory(root: Path) -> dict[str, Any]:
    paths = [root / name for name in MARKERS]
    for name in ARTIFACT_DIRS:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("RELEASE_ARTIFACT_DIRECTORY")
        paths.extend(directory.rglob("*"))
    result: dict[str, Any] = {}
    for path in sorted(paths):
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = {"target": os.readlink(path),
                                "sha256": _sha(path) if path.is_file() else None}
        elif path.is_file():
            result[relative] = {"sha256": _sha(path), "mode": path.stat().st_mode & 0o777}
        elif relative in MARKERS:
            raise ValueError("RELEASE_MARKER_MISSING")
    return result


def seal_bundle(root: Path) -> dict[str, Any]:
    commit = (root / "COMMIT").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("RELEASE_COMMIT")
    value = {"schema": "prop_release_bundle_v1", "application_commit": commit,
             "compatibility_sha256": _sha(root / "compatibility.json"),
             "files": _inventory(root), "trading_authorized": False}
    _atomic_json(root / "BUNDLE.json", value)
    return verify_bundle(root, expected_commit=commit)


def verify_bundle(root: Path, *, expected_commit: str | None = None) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("RELEASE_DIRECTORY")
    value = json.loads((root / "BUNDLE.json").read_text())
    commit = (root / "COMMIT").read_text().strip()
    compatibility = json.loads((root / "compatibility.json").read_text())
    if (value.get("schema") != "prop_release_bundle_v1"
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or value.get("application_commit") != commit
            or expected_commit is not None and commit != expected_commit
            or value.get("trading_authorized") is not False
            or compatibility["contracts"]["execution_mode"] != "SHADOW"
            or compatibility["contracts"]["trading_authorized"] is not False
            or value.get("compatibility_sha256") != _sha(root / "compatibility.json")
            or (root / "SOURCE.sha256").read_text().split()[0] != _sha(root / "SOURCE.tar")
            or not os.access(root / "venv/bin/python", os.X_OK)):
        raise ValueError("RELEASE_IDENTITY_OR_CONTRACT")
    if value.get("files") != _inventory(root):
        raise ValueError("RELEASE_CONTENT_DRIFT")
    for unit in UNITS:
        if not (root / "units" / unit).is_file():
            raise ValueError("RELEASE_UNIT_MISSING")
    return dict(value)


def backup_database(source: Path, target: Path) -> None:
    """SQLite online backup includes committed WAL frames without touching the source."""
    if source.is_symlink() or not source.is_file() or target.exists():
        raise ValueError("DATABASE_BACKUP_PATH")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if (dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or dst.execute("PRAGMA foreign_key_check").fetchall()):
                raise ValueError("DATABASE_BACKUP_INTEGRITY")


def _systemctl(arguments: list[str]) -> None:
    subprocess.run(["systemctl", *arguments], check=True, timeout=90, capture_output=True)


def _pointer(opt: Path, release: Path | None) -> None:
    current = opt / "current"
    if release is None:
        current.unlink(missing_ok=True)
    else:
        temporary = opt / "current.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(release)
        os.replace(temporary, current)
    descriptor = os.open(opt, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore(opt: Path, unit_dir: Path, record: dict[str, Any], service: ServiceCall) -> None:
    service(["stop", *UNITS])
    for unit, existed in record["units"].items():
        target = unit_dir / unit
        if existed:
            shutil.copy2(Path(record["backup"]) / unit, target)
        else:
            target.unlink(missing_ok=True)
    previous = Path(record["previous"]) if record["previous"] else None
    _pointer(opt, previous)
    service(["daemon-reload"])
    if record["previous_active"]:
        service(["start", *record["previous_active"]])
        service(["is-active", "--quiet", *record["previous_active"]])


def recover_activation(opt: Path, unit_dir: Path, *, service: ServiceCall = _systemctl) -> None:
    record_path = opt / "activation.json"
    record = json.loads(record_path.read_text())
    if record["phase"] not in {"PREPARED", "ROLLBACK_FAILED"}:
        raise ValueError("NO_INTERRUPTED_ACTIVATION")
    _restore(opt, unit_dir, record, service)
    record["phase"] = "ROLLED_BACK"
    _atomic_json(record_path, record)


def activate_release(
    opt: Path, unit_dir: Path, release: Path, *,
    previous_active: list[str], health_check: Callable[[], None],
    service: ServiceCall = _systemctl,
) -> None:
    """Snapshot units and pointer; roll back the exact code set after a failed start."""
    if release.is_symlink() or release.resolve().parent != (opt / "releases").resolve():
        raise ValueError("RELEASE_OUTSIDE_ROOT")
    verify_bundle(release, expected_commit=release.name)
    record_path = opt / "activation.json"
    if record_path.exists():
        existing = json.loads(record_path.read_text())
        if existing["phase"] in {"PREPARED", "ROLLBACK_FAILED"}:
            raise ValueError("INTERRUPTED_ACTIVATION_RECOVERY_REQUIRED")
    if set(previous_active) - set(UNITS):
        raise ValueError("UNKNOWN_PREVIOUS_SERVICE")
    current = opt / "current"
    previous = current.resolve(strict=True) if current.is_symlink() else None
    if previous is not None and previous.parent != (opt / "releases").resolve():
        raise ValueError("PREVIOUS_RELEASE_OUTSIDE_ROOT")
    if any((unit_dir / unit).is_symlink() for unit in UNITS):
        raise ValueError("UNIT_PATH_IS_SYMLINK")
    if current.exists() and previous is None:
        raise ValueError("CURRENT_NOT_SYMLINK")
    backup = opt / "activation-backups" / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    units = {unit: (unit_dir / unit).exists() for unit in UNITS}
    for unit, existed in units.items():
        if existed:
            shutil.copy2(unit_dir / unit, backup / unit)
    record = {"phase": "PREPARED", "candidate": str(release),
              "previous": str(previous) if previous else None,
              "previous_active": previous_active, "backup": str(backup), "units": units}
    _atomic_json(record_path, record)
    try:
        service(["stop", *UNITS])
        for unit in UNITS:
            shutil.copy2(release / "units" / unit, unit_dir / unit)
        _pointer(opt, release)
        service(["daemon-reload"])
        service(["start", *UNITS])
        service(["is-active", "--quiet", *UNITS])
        health_check()
    except BaseException:
        try:
            _restore(opt, unit_dir, record, service)
            record["phase"] = "ROLLED_BACK"
        except BaseException:
            record["phase"] = "ROLLBACK_FAILED"
            raise
        finally:
            _atomic_json(record_path, record)
        raise
    record["phase"] = "ACTIVE"
    _atomic_json(record_path, record)


@contextmanager
def _release_lock(opt: Path, inherited_fd: int | None = None) -> Iterator[None]:
    """Serialize manual recovery and activation with the installer's stage lock."""
    lock = opt / ".deployment.lock"
    descriptor = inherited_fd
    if descriptor is None:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        opened, named = os.fstat(descriptor), lock.stat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("DEPLOYMENT_LOCK_IDENTITY")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if inherited_fd is None:
            os.close(descriptor)


def _fresh_heartbeats(state: Path, started: float) -> None:
    paths = [state / "health" / role / "heartbeat.json"
             for role in ("guardian", "observer", "bybit")]
    paths.append(state / "watchdog" / "heartbeat.json")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if all(path.is_file() and path.stat().st_mtime >= started for path in paths):
            try:
                values = [json.loads(path.read_text()) for path in paths]
            except (OSError, json.JSONDecodeError):
                # The legacy Propr observer writes in place. A partial read must
                # wait for its completed heartbeat within the same bounded gate.
                time.sleep(0.25)
                continue
            if any(value.get("trading_authorized") is True for value in values):
                raise ValueError("UNEXPECTED_TRADING_AUTHORITY")
            return
        time.sleep(0.25)
    raise ValueError("FRESH_ROLE_HEARTBEATS_MISSING")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "verify", "activate", "recover", "backup"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.action == "verify":
        result = verify_bundle(args.path, expected_commit=args.commit)
        print(json.dumps({"verified": True, "commit": result["application_commit"],
                          "files": len(result["files"]), "trading_authorized": False}))
    elif args.action == "seal":
        seal_bundle(args.path)
    elif args.action == "backup":
        if args.target is None:
            parser.error("--target required")
        backup_database(args.path, args.target)
    else:
        if os.geteuid() != 0:
            raise PermissionError("ROOT_RELEASE_AUTHORITY_REQUIRED")
        opt, unit_dir = Path("/opt/prop-production"), Path("/etc/systemd/system")
        with _release_lock(opt, args.lock_fd):
            if args.action == "recover":
                if args.path.resolve() != opt:
                    raise ValueError("RECOVERY_ROOT")
                recover_activation(opt, unit_dir)
            else:
                for item in [args.path, *args.path.rglob("*")]:
                    protected = item.resolve().stat()
                    if protected.st_uid != 0 or protected.st_mode & 0o022:
                        raise PermissionError("RELEASE_NOT_ROOT_PROTECTED")
                active = [unit for unit in UNITS if subprocess.run(
                    ["systemctl", "is-active", "--quiet", unit], check=False
                ).returncode == 0]
                started = time.time()
                activate_release(opt, unit_dir, args.path, previous_active=active,
                                 health_check=lambda: _fresh_heartbeats(
                                     Path("/var/lib/prop-production"), started))
        print("RELEASE_OPERATION_VERIFIED; SHADOW / NO_TRADE")


if __name__ == "__main__":
    main()
