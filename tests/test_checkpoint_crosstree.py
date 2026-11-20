"""S3: exact producer process, sixteen paper journals and protected SHADOW."""

import json
import os
import subprocess
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest
from checkpoint_scenarios import scenario

from prop_production.compatibility import MANIFEST, RESEARCH_COMMIT
from prop_production.gateway import ReadOnlyGateway


def test_checkpoint_compatibility_contracts_are_explicit():
    contracts = MANIFEST["contracts"]
    assert contracts["portfolio_release_versions"] == [
        "portfolio_release_v2",
    ]
    assert contracts["checkpoint_source"] == "cwsa_checkpoint_packet_v3"
    assert contracts["copy_mandate"] == "copy_mandate_v1"
    assert contracts["active_source_wire"] == 3
    assert contracts["trading_authorized"] is False


def test_exact_producer_to_sixteen_paper_consumers_and_guardian_readonly_shadow(tmp_path):
    configured = os.environ.get("PROP_RESEARCH_ROOT")
    if not configured:
        pytest.skip("Set PROP_RESEARCH_ROOT to the exact clean companion pin")
    root = Path(configured)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        == RESEARCH_COMMIT
    )
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip()
    exported = subprocess.run(
        [
            str(root / ".venv/bin/python"),
            "-c",
            "import runpy,sys; p=sys.argv.pop(1); runpy.run_path(p, run_name='__main__')",
            str(Path(__file__).with_name("checkpoint_paper_producer.py")),
            str(tmp_path / "producer"),
        ],
        cwd=root,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"))),
        },
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert exported.returncode == 0, exported.stderr
    result = json.loads(exported.stdout)
    assert result["producer_revision"] == RESEARCH_COMMIT
    assert len(result["paper"]) == 16
    assert result["baseline_statuses"] == ["BASELINE"] * 16
    assert result["source_transition_count"] == 3
    assert len({row["signal_id"] for row in result["paper"].values()}) == 1
    assert len({row["journal_head_sha"] for row in result["paper"].values()}) == 16
    assert result["restart_exact"] is True
    assert result["financial_mode"] == "SYNTHETIC_PAPER_NO_BROKER_ORDERS"
    step = Decimal("0.00001")
    for row in result["paper"].values():
        full_target = Decimal(row["k"]) * Decimal("3") * Decimal("10000") / Decimal("10001")
        rounded = (full_target / step).to_integral_value(rounding=ROUND_DOWN) * step
        assert Decimal(row["requested_target"]) == rounded
        assert row["status"] == "COMMITTED_ONCE"
        assert Decimal(row["costs"]) > 0
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/cwsa_checkpoint_vectors_v3.json").read_text()
    )
    assert result["packets"] == fixture["sources"][0]["packets"][:2]

    with scenario(tmp_path / "guardian", copies=((0, "0.001"),)) as s:
        # Re-ingesting the actual producer's packets must be idempotent.
        for packet in result["packets"]:
            s.receiver.ingest(json.dumps(packet).encode(), now_ms=s.now)
        assert s.receiver.due(now_ms=s.now) == []
        s.guardian.reconcile(s.now)
        quotes = {
            "BTC": {"bid": "9999", "ask": "10001", "known_at_ms": s.now},
            "ETH": {"bid": "999", "ask": "1001", "known_at_ms": s.now},
        }
        reviewed = s.guardian.review_checkpoints(
            s.receiver, s.requests, quotes=quotes, now_ms=s.now
        )
        assert reviewed["status"] == "REVIEWABLE"
        paper = next(r for r in result["paper"].values() if r["k"] == "0.001")
        assert Decimal(reviewed["risk"]["pod_targets"][s.mandates[0].owner]["BTC"]) == Decimal(
            paper["requested_target"]
        )
        assert s.now - result["checkpoint_ms"] < 300000
        assert not s.journal.snapshot()["intents"]
        assert not s.broker.audit()
        # The actual protected read-only boundary cannot turn a reviewed fixture into an order.
        s.guardian.gateway = ReadOnlyGateway(
            lambda: s.broker.observe(s.now), "fixture-account", "fake://broker"
        )
        with pytest.raises(PermissionError, match="REMOTE_MUTATION"):
            s.guardian.reserve_checkpoint(
                reviewed["decision_id"], receiver=s.receiver, now_ms=s.now + 1
            )
        assert not s.journal.snapshot()["intents"]
        assert not s.broker.audit()
        s.journal.verify()


def test_research_build_manifest_v2_portfolio_release_v2_and_shadow_candidate(tmp_path):
    from copy import deepcopy
    from hashlib import sha256

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from prop_production.evidence import sign
    from prop_production.release import ReleaseFloor, ReleaseVerifier
    from prop_production.shadow_release import verify_candidate

    configured = os.environ.get("PROP_RESEARCH_ROOT")
    if not configured:
        pytest.skip("Set PROP_RESEARCH_ROOT to the exact clean companion pin")
    root = Path(configured)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        == RESEARCH_COMMIT
    )
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip()
    script = """
import json, sys, tempfile
from pathlib import Path
from copytrade_pm.production_manifest import build_manifest_v2
from test_production_manifest import fixture_v2_args

with tempfile.TemporaryDirectory() as directory:
    args = fixture_v2_args(Path(directory))
    effective_from_ms = ((args["now_ms"] // 300_000) + 1) * 300_000
    for mandate in args["mandates"]:
        mandate["effective_from_ms"] = effective_from_ms
    manifest = build_manifest_v2(**args)
    print(json.dumps({
        "manifest": manifest,
        "reviewer_private_hex": args["release_signer"].private_bytes_raw().hex(),
        "reviewer_public_hex": args["reviewer"].public_bytes_raw().hex(),
        "source_public_hex": args["source_observer"].public_bytes_raw().hex(),
        "now_ms": args["now_ms"],
        "effective_from_ms": effective_from_ms,
    }))
"""
    proc = subprocess.run(
        [str(root / ".venv/bin/python"), "-c", script],
        cwd=root,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"))),
        },
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    exported = json.loads(proc.stdout)
    manifest = exported["manifest"]
    payload = manifest["payload"]
    reviewer_pub = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(exported["reviewer_public_hex"])
    )
    source_pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(exported["source_public_hex"]))

    verifier = ReleaseVerifier(
        reviewer_pub,
        "approver",
        payload["account_binding"],
        payload["risk_policy_sha"],
        payload["rulebook_sha"],
    )
    verified = verifier.verify(manifest, now_ms=exported["now_ms"])
    assert verified.payload["schema"] == "portfolio_release_v2"
    assert verified.trading_authorized is False

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    reviewer_priv = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(exported["reviewer_private_hex"])
    )
    expected_compat = deepcopy(MANIFEST)
    expected_compat["repositories"]["copytrade-pm"]["commit"] = verified.payload["research_commit"]
    reviewer_sha = sha256(reviewer_pub.public_bytes_raw()).hexdigest()
    app_commit = "a" * 40
    candidate_body = {
        "schema": "cwsa_shadow_candidate_v1",
        "release": manifest,
        "compatibility": expected_compat,
        "application_commit": app_commit,
        "trust_roots": {
            "release": reviewer_sha,
            "mandate": reviewer_sha,
            "source": sha256(source_pub.public_bytes_raw()).hexdigest(),
        },
        "created_at_ms": exported["now_ms"],
        "effective_from_ms": exported["effective_from_ms"],
        "mode": "SHADOW_ONLY",
        "trading_authorized": False,
    }
    envelope = sign(candidate_body, reviewer_priv, "cwsa-shadow-candidate-v1")
    with ReleaseFloor(tmp_path / "floor.sqlite", initialize=True) as floor:
        candidate = verify_candidate(
            envelope,
            verifier=verifier,
            source_key=source_pub,
            floor=floor,
            expected_compatibility=expected_compat,
            application_commit=app_commit,
            now_ms=exported["now_ms"],
        )
        assert candidate.transition.successor == "SHADOW_VERIFIED"
        assert candidate.transition.effect == "OBSERVATION_ONLY"
        assert candidate.release.trading_authorized is False
        assert floor.con.execute("SELECT * FROM releases").fetchall() == []
        with pytest.raises(ValueError, match="NOT_ACTIVATED"):
            floor.require_active(candidate.release)
