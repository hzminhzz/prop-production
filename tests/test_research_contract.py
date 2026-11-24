"""Protected research checkout, and the PM-signed V2 release acceptance proof.

Phase D pins the exact sibling worktree ``../copytrade-pm-phase-d`` (the same
topology the path-sourced ``copytrade-kernel`` dependency already requires).
Gate finding F2 therefore restores the cross-repository release-acceptance
proof as an unconditional test over synthetic fixtures: it fails loudly when
the companion is missing instead of skipping behind an unset pin. The companion
is imported, never written: bytecode writing is disabled for the child.
"""

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest


def test_research_checkout_rejects_untracked_importable_modules(tmp_path, monkeypatch):
    from prop_production import research_bridge

    root = tmp_path / "pinned-research"
    root.mkdir()
    (root / "src").mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr(research_bridge, "RESEARCH_COMMIT", revision)
    research_bridge.verify_checkout(root)
    (root / "src" / "untracked_runtime.py").write_text("UNREVIEWED = True\n")
    with pytest.raises(ValueError, match="RESEARCH_REVISION_NOT_PINNED"):
        research_bridge.verify_checkout(root)


def companion_root() -> Path:
    pinned = os.environ.get("PROP_RESEARCH_ROOT")
    if pinned:
        return Path(pinned).expanduser().resolve()
    dev_root = Path(__file__).resolve().parents[2]
    sibling = dev_root / "copytrade-pm"
    if sibling.is_dir():
        return sibling
    return dev_root / "copytrade-pm-phase-d"


COMPANION_PROGRAM = r"""
import json, tempfile
from pathlib import Path

from copytrade_pm.production_manifest import build_manifest_v2
from test_production_manifest import fixture_v2_args

with tempfile.TemporaryDirectory() as directory:
    args = fixture_v2_args(Path(directory))
    manifest = build_manifest_v2(**args)
    print(json.dumps({
        "manifest": manifest,
        "public": args["reviewer"].public_bytes_raw().hex(),
        "now_ms": args["now_ms"],
        "release_signer_public": args["release_signer"].public_key().public_bytes_raw().hex(),
    }))
"""


def test_pm_signed_v2_release_is_accepted_by_the_production_verifier():
    """F2: a copytrade-pm-signed V2 manifest must pass the production verifier."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from prop_production.release import ReleaseVerifier

    companion = companion_root()
    interpreter = companion / ".venv" / "bin" / "python"
    assert (companion / "src" / "copytrade_pm" / "production_manifest.py").is_file(), (
        f"pinned companion worktree missing: {companion}"
    )
    assert interpreter.is_file(), f"pinned companion interpreter missing: {interpreter}"
    process = subprocess.run(
        [str(interpreter), "-c", COMPANION_PROGRAM],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": f"{companion / 'src'}:{companion / 'tests'}",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    evidence = json.loads(process.stdout)
    signed = evidence["manifest"]
    payload = signed["payload"]
    verified = ReleaseVerifier(
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(evidence["public"])),
        "approver",
        payload["account_binding"],
        payload["risk_policy_sha"],
        payload["rulebook_sha"],
    ).verify(signed, now_ms=evidence["now_ms"])

    assert verified.payload["schema"] == "portfolio_release_v2"
    assert verified.payload == payload
    assert verified.release_sha == signed["sha256"]
    assert verified.trading_authorized is False
    assert verified.signed_envelope is not None
    assert len(verified.payload["members"]) == 3
    assert verified.payload["batch_seconds"] == 300
    assert all(
        member["sizing_policy"] == "FIXED_K_NATIVE_QUANTITY" and member["k"] == "0.04"
        for member in verified.payload["members"]
    )
    assert len(verified.payload["mandates"]) == len(verified.payload["members"])
    assert all(mandate["k"] == "0.04" for mandate in verified.payload["mandates"])
    assert (
        verified.payload["mandate_authority_sha"]
        == sha256(bytes.fromhex(evidence["release_signer_public"])).hexdigest()
    )
