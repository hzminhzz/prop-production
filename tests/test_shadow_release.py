"""Nonactivating signed candidates bind external trust, future boundary and exact code."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_checkpoint_receiver import KEY
from test_copy_mandates import START, release_body, verifier

from prop_production.compatibility import MANIFEST
from prop_production.evidence import digest, sign
from prop_production.release import ReleaseFloor
from prop_production.shadow_release import verify_candidate


def candidate(key):
    release = release_body(key)
    release["research_commit"] = MANIFEST["repositories"]["copytrade-pm"]["commit"]
    release["source_sha"] = digest(
        {
            k: release[k]
            for k in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    return {
        "schema": "cwsa_shadow_candidate_v1",
        "release": sign(release, key, "approver"),
        "compatibility": deepcopy(MANIFEST),
        "application_commit": "a" * 40,
        "trust_roots": {
            "release": sha256(key.public_key().public_bytes_raw()).hexdigest(),
            "mandate": sha256(key.public_key().public_bytes_raw()).hexdigest(),
            "source": sha256(KEY.public_key().public_bytes_raw()).hexdigest(),
        },
        "created_at_ms": START - 10,
        "effective_from_ms": START,
        "mode": "SHADOW_ONLY",
        "trading_authorized": False,
    }


def check(body, key, floor, now=START - 1):
    return verify_candidate(
        sign(body, key, "cwsa-shadow-candidate-v1"),
        verifier=verifier(key),
        source_key=KEY.public_key(),
        floor=floor,
        expected_compatibility=MANIFEST,
        application_commit="a" * 40,
        now_ms=now,
    )


def test_candidate_verification_does_not_activate_or_advance_floor(tmp_path):
    key = Ed25519PrivateKey.generate()
    with ReleaseFloor(tmp_path / "floor.sqlite", initialize=True) as floor:
        before = (tmp_path / "floor.sqlite").read_bytes()
        body = candidate(key)
        receipt = check(body, key, floor)
        assert (
            receipt.release.payload["research_commit"]
            == MANIFEST["repositories"]["copytrade-pm"]["commit"]
        )
        assert receipt.transition.successor == "SHADOW_VERIFIED"
        assert receipt.transition.effect == "OBSERVATION_ONLY"
        assert receipt.transition.evidence_sha == digest(body)
        assert asdict(receipt.transition)["guard"] == "EXACT_PINS_TRUST_FUTURE_BOUNDARY"
        assert (tmp_path / "floor.sqlite").read_bytes() == before
        assert floor.con.execute("SELECT * FROM releases").fetchall() == []
        with pytest.raises(ValueError, match="NOT_ACTIVATED"):
            floor.require_active(receipt.release)


@pytest.mark.parametrize(
    "fault",
    [
        "code",
        "research",
        "connector",
        "source_trust",
        "scope",
        "boundary",
        "clock",
        "permission",
        "extra",
    ],
)
def test_signed_candidate_still_rejects_mismatch(tmp_path, fault):
    key = Ed25519PrivateKey.generate()
    body = candidate(key)
    if fault == "code":
        body["application_commit"] = "b" * 40
    elif fault in {"research", "connector"}:
        repo = "copytrade-pm" if fault == "research" else "trading-connector"
        body["compatibility"]["repositories"][repo]["commit"] = "b" * 40
    elif fault == "source_trust":
        body["trust_roots"]["source"] = "b" * 64
    elif fault == "scope":
        raw = body["release"]["payload"]
        raw["account_binding"] = "another-account"
        body["release"] = sign(raw, key, "approver")
    elif fault == "boundary":
        body["effective_from_ms"] += 1
    elif fault == "clock":
        body["created_at_ms"] = START + 1
    elif fault == "permission":
        body["trading_authorized"] = True
    else:
        body["ready"] = True
    with ReleaseFloor(tmp_path / "floor.sqlite", initialize=True) as floor:
        with pytest.raises(ValueError):
            check(body, key, floor)
        assert floor.con.execute("SELECT * FROM releases").fetchall() == []


def test_candidate_expiry_equality_and_signer_floor_are_fail_closed(tmp_path):
    key = Ed25519PrivateKey.generate()
    body = candidate(key)
    with ReleaseFloor(tmp_path / "floor.sqlite", initialize=True) as floor:
        with pytest.raises(ValueError, match="FUTURE_BOUNDARY"):
            check(body, key, floor, now=START)
        high = deepcopy(body["release"]["payload"])
        high["generation"] += 1
        floor.accept(verifier(key).verify(sign(high, key, "approver"), now_ms=START - 1))
        before = (tmp_path / "floor.sqlite").read_bytes()
        with pytest.raises(ValueError, match="ROLLBACK"):
            check(body, key, floor)
        assert (tmp_path / "floor.sqlite").read_bytes() == before
