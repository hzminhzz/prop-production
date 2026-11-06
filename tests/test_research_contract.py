import json
import os
import subprocess
from pathlib import Path

import pytest


def test_research_checkout_rejects_untracked_importable_modules(tmp_path, monkeypatch):
    from prop_production import research_bridge

    root = tmp_path / "pinned-research"
    root.mkdir()
    (root / "src").mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
                    "-qm", "initial"], check=True)
    revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(research_bridge, "RESEARCH_COMMIT", revision)
    research_bridge.verify_checkout(root)
    (root / "src" / "untracked_runtime.py").write_text("UNREVIEWED = True\n")
    with pytest.raises(ValueError, match="RESEARCH_REVISION_NOT_PINNED"):
        research_bridge.verify_checkout(root)


@pytest.mark.parametrize("policy_name", ["ALLOCATION_OVER_LIVE_NAV", "FIXED_K_NATIVE_QUANTITY"])
def test_actual_research_sqlite_export_is_accepted_by_production_verifier(policy_name):
    root = os.getenv("PROP_RESEARCH_ROOT")
    if not root:
        pytest.skip("Explicit pinned research checkout required for cross-repository integration")
    research = Path(root)
    from prop_production.research_bridge import verify_checkout
    verify_checkout(research)
    program = """
import json, sys, tempfile
from pathlib import Path
from test_production_manifest import fixture_policy
from copytrade_pm.production_manifest import store_policy, build_manifest, sign
with tempfile.TemporaryDirectory() as directory:
    mode = sys.argv[1]
    db, metadata, policy, reviewer, source = fixture_policy(Path(directory), sizing_policy=mode)
    if mode == 'FIXED_K_NATIVE_QUANTITY':
        body = policy['payload']
        for member in body['members']:
            member['sizing_policy'] = mode
            member['k'] = '0.04'
        for index, envelope in enumerate(body['source_attestations']):
            proof = envelope['payload']
            proof['nav'] = None
            body['source_attestations'][index] = sign(proof, source, 'source-observer')
        policy = sign(body, reviewer, 'research-release-policy')
    store_policy(db, 'A', metadata, policy, reviewer.public_key())
    manifest = build_manifest(db=db, portfolio_id='A', asof='2026-11-04T06:00:00+00:00',
        reviewer=reviewer.public_key(), source_observer=source.public_key(),
        release_signer=reviewer, now_ms=metadata['asof_ms'])
    print(json.dumps({'manifest':manifest, 'public':reviewer.public_key().public_bytes_raw().hex(),
                      'now_ms':metadata['asof_ms'],
                      'source_nav_values':[x['payload']['nav'] for x in policy['payload']['source_attestations']]}))
"""
    process = subprocess.run([str(research / ".venv/bin/python"), "-c", program, policy_name], check=True,
                             capture_output=True, text=True, timeout=30,
                             env={"PATH": os.defpath, "PYTHONPATH": str(research / "src") + ":" + str(research / "tests")})
    evidence = json.loads(process.stdout)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from prop_production.release import ReleaseVerifier
    payload = evidence["manifest"]["payload"]
    verifier = ReleaseVerifier(Ed25519PublicKey.from_public_bytes(bytes.fromhex(evidence["public"])),
                               "approver", "fixture-account",
                               payload["risk_policy_sha"], payload["rulebook_sha"])
    verified = verifier.verify(evidence["manifest"], now_ms=evidence["now_ms"])
    assert len(verified.payload["members"]) == 3
    assert verified.payload["batch_seconds"] == 300
    assert verified.trading_authorized is False

    assert all(member["sizing_policy"] == policy_name for member in verified.payload["members"])
    if policy_name == "FIXED_K_NATIVE_QUANTITY":
        assert all(nav is None for nav in evidence["source_nav_values"])
        assert all(member["k"] == "0.04" for member in verified.payload["members"])
