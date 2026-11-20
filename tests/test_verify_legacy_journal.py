"""Standalone archive verifier must leave all evidence bytes and names intact."""

import ast
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from checkpoint_archive_fixture import presemantic_archive, restore, v1_release_body
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prop_production.evidence import canonical, digest, sign

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_legacy_v1_journal.py"
PROTECTED = Path(__file__).resolve().parents[1] / "src" / "prop_production"
NOW_MS = 1_794_441_900_000
AUTHORITY = "release-approver"


def run_archive(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True
    )


def signed_v1(tmp_path, *, sizing_policy="FIXED_K_NATIVE_QUANTITY", k="0.04"):
    key = Ed25519PrivateKey.generate()
    payload = v1_release_body(now_ms=NOW_MS, sizing_policy=sizing_policy, k=k)
    envelope = sign(payload, key, AUTHORITY)
    evidence = tmp_path / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "release.json").write_bytes(canonical(envelope))
    (evidence / "release.pub").write_text(key.public_key().public_bytes_raw().hex() + "\n")
    return payload, envelope, key, evidence


def release_flags(payload, evidence):
    return (
        "--release",
        evidence / "release.json",
        "--public-key",
        evidence / "release.pub",
        "--authority",
        AUTHORITY,
        "--account-binding",
        payload["account_binding"],
        "--risk-policy-sha",
        payload["risk_policy_sha"],
        "--rulebook-sha",
        payload["rulebook_sha"],
        "--now-ms",
        str(NOW_MS),
    )


def archive_with(tmp_path, release_sha):
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    return archive, presemantic_archive(archive, release_sha=release_sha)


@pytest.mark.parametrize("fixture", ["guardian_portfolio_v1", "checkpoint_q15_legacy_v2"])
def test_archive_verifies_read_only(tmp_path, fixture):
    path = restore(tmp_path, fixture)
    path.chmod(0o400)
    before = path.read_bytes()
    result = run_archive(path)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["legacy_events"] == 0
    assert evidence["integrity"] == evidence["financial_semantics"] == "VERIFIED"
    assert evidence["release_validation"] == "NOT_EVIDENCED"
    assert evidence["order_route"] == "NONE"
    assert evidence["trading_authorized"] is False
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == before


@pytest.mark.parametrize("tamper", ["snapshot", "wal"])
def test_archive_fails_closed(tmp_path, tamper):
    path = restore(tmp_path, "guardian_portfolio_v1")
    if tamper == "snapshot":
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE snapshot SET sha = 'tampered'")
    else:
        path.with_name(path.name + "-wal").write_bytes(b"unfinished")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = run_archive(path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_presemantic_archive_reports_hash_only_evidence(tmp_path):
    payload = v1_release_body(now_ms=NOW_MS)
    archive, path = archive_with(tmp_path, digest(payload))
    before = path.read_bytes()
    result = run_archive(path)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["sequence"] == 3
    assert evidence["legacy_events"] == 3
    assert evidence["semantic_events"] == 0
    assert evidence["financial_semantics"] == "LEGACY_CHAIN_ONLY"
    assert evidence["release_validation"] == "NOT_EVIDENCED"
    assert evidence["archive_release_shas"] == [digest(payload)]
    assert "release_sha" not in evidence
    assert evidence["order_route"] == "NONE"
    assert evidence["trading_authorized"] is False
    assert path.read_bytes() == before
    assert list(archive.iterdir()) == [path]


def test_archive_verifies_original_signed_v1_release(tmp_path):
    payload, envelope, _, evidence = signed_v1(tmp_path)
    archive, path = archive_with(tmp_path, digest(payload))
    before = path.read_bytes()
    result = run_archive(path, *release_flags(payload, evidence))
    assert result.returncode == 0, result.stderr
    attestation = json.loads(result.stdout)
    assert attestation["release_validation"] == "VERIFIED"
    assert attestation["release_schema"] == "portfolio_release_v1"
    assert attestation["release_sha"] == digest(payload) == envelope["sha256"]
    assert attestation["release_generation"] == 1
    assert attestation["release_authority"] == AUTHORITY
    assert attestation["release_envelope_sha"] == digest(envelope)
    assert attestation["sizing_policies"] == ["FIXED_K_NATIVE_QUANTITY"]
    assert attestation["order_route"] == "NONE"
    assert attestation["trading_authorized"] is False
    assert path.read_bytes() == before
    assert list(archive.iterdir()) == [path]


def test_archive_preserves_historical_nav_semantics_without_claiming_them_live(tmp_path):
    payload, envelope, key, evidence = signed_v1(tmp_path, sizing_policy="ALLOCATION_OVER_LIVE_NAV")
    _, path = archive_with(tmp_path, digest(payload))
    result = run_archive(path, *release_flags(payload, evidence))
    assert result.returncode == 0, result.stderr
    attestation = json.loads(result.stdout)
    assert attestation["release_validation"] == "VERIFIED"
    assert attestation["sizing_policies"] == ["ALLOCATION_OVER_LIVE_NAV"]
    assert attestation["trading_authorized"] is False
    from prop_production.release import ReleaseVerifier

    with pytest.raises(ValueError, match="UNSUPPORTED_RELEASE_SCHEMA"):
        ReleaseVerifier(
            key.public_key(),
            AUTHORITY,
            payload["account_binding"],
            payload["risk_policy_sha"],
            payload["rulebook_sha"],
        ).verify(envelope, now_ms=NOW_MS)


def test_archive_refuses_partial_release_evidence(tmp_path):
    payload, _, _, evidence = signed_v1(tmp_path)
    archive, path = archive_with(tmp_path, digest(payload))
    before = path.read_bytes()
    result = run_archive(
        path, "--release", evidence / "release.json", "--public-key", evidence / "release.pub"
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "ARCHIVE_RELEASE_EVIDENCE_INCOMPLETE" in result.stderr
    assert path.read_bytes() == before
    assert list(archive.iterdir()) == [path]


@pytest.mark.parametrize("fault", ["wrong-key", "tampered-payload", "tampered-signature", "unbound"])
def test_archive_refuses_untrusted_release_evidence(tmp_path, fault):
    payload, envelope, _, evidence = signed_v1(tmp_path)
    bound = digest(payload)
    if fault == "wrong-key":
        other = Ed25519PrivateKey.generate()
        (evidence / "release.pub").write_text(other.public_key().public_bytes_raw().hex() + "\n")
    elif fault in {"tampered-payload", "tampered-signature"}:
        envelope["payload"]["members"][0]["allocation"] = "999999"
        if fault == "tampered-signature":
            envelope["sha256"] = digest(envelope["payload"])
        (evidence / "release.json").write_bytes(canonical(envelope))
    else:
        bound = "f" * 64
    _, path = archive_with(tmp_path, bound)
    result = run_archive(path, *release_flags(payload, evidence))
    assert result.returncode == 2
    assert result.stdout == ""


def test_archive_refuses_tampered_presemantic_row(tmp_path):
    payload = v1_release_body(now_ms=NOW_MS)
    _, path = archive_with(tmp_path, digest(payload))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE events SET kind = 'RECONCILE_TAMPERED' WHERE seq = 3")
    result = run_archive(path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "JOURNAL_TAMPER" in result.stderr


def test_no_production_importer_of_the_archive_script():
    """C5: the archival copy is additive; ``prop_production`` must not reach it."""
    forbidden = SCRIPT.name
    offenders: list[str] = []
    for path in sorted(PROTECTED.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == forbidden or name.split(".")[0] == "scripts" for name in names):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_archive_script_stays_outside_the_package_boundary():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.add((node.module or "").split(".")[0])
    assert modules <= {
        "__future__",
        "argparse",
        "contextlib",
        "cryptography",
        "copytrade_kernel",
        "dataclasses",
        "json",
        "pathlib",
        "prop_production",
        "re",
        "sqlite3",
        "typing",
    }
    assert "scripts" not in modules
