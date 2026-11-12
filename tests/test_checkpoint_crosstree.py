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
        "portfolio_release_v1",
        "portfolio_release_v2",
    ]
    assert contracts["checkpoint_source"] == "cwsa_checkpoint_packet_v3"
    assert contracts["copy_mandate"] == "copy_mandate_v1"
    assert contracts["active_source_wire"] == 1
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
