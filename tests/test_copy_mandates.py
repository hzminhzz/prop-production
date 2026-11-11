"""Independent protected verification of immutable checkpoint Copy Mandates."""

from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_checkpoint_receiver import POLICY, SCOPE

from prop_production.evidence import digest, sign
from prop_production.release import ReleaseVerifier, fixture_release

START = 1_794_441_900_000


def mandate_body(generation=1, start=START):
    return {
        "schema": "copy_mandate_v1",
        "mandate_id": "fixture-copy",
        "generation": generation,
        "source_id": SCOPE["source_id"],
        "source_scope_generation": SCOPE["generation"],
        "source_account_sha": SCOPE["account_sha"],
        "account_id": "fixture-account",
        "pod_id": "pod-a",
        "k": "0.025",
        "allowed_markets": ["hyperliquid/mainnet:perp:default/BTC"],
        "policy_epoch": digest(POLICY),
        "risk_policy_sha": digest("synthetic-risk"),
        "rulebook_sha": digest("synthetic-rules"),
        "effective_from_ms": start,
        "effective_until_ms": start + 3_600_000,
        "sizing_policy": "FIXED_K_NATIVE_QUANTITY",
        "entry_policy": "ONE_SHOT_CHECKPOINT_V3",
        "exit_policy": "NATIVE_SAFE_CROSS_OR_REDUCE_V3",
    }


def release_body(key, mandate=None):
    m = mandate or mandate_body()
    p = fixture_release()
    p.update(
        schema="portfolio_release_v2",
        created_at_ms=START - 1000,
        selection_known_at_ms=START - 1001,
        expires_at_ms=START + 3_600_001,
        checkpoint_policy=POLICY,
        checkpoint_scopes={SCOPE["source_id"]: SCOPE},
        mandates=[m],
        mandate_authority_sha=sha256(key.public_key().public_bytes_raw()).hexdigest(),
    )
    member = p["members"][0]
    member.update(
        source_identity=m["source_id"],
        account_scope=[m["source_id"]],
        identity_known_at_ms=START - 1002,
        sizing_policy="FIXED_K_NATIVE_QUANTITY",
        k=m["k"],
        allowed_markets=m["allowed_markets"],
    )
    p["definition_sha"] = digest(p["members"])
    p["source_sha"] = digest(
        {
            k: p[k]
            for k in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    return p


def verifier(key):
    return ReleaseVerifier(
        key.public_key(),
        "approver",
        "fixture-account",
        digest("synthetic-risk"),
        digest("synthetic-rules"),
    )


def test_new_signed_release_preserves_distinct_legacy_contract():
    key = Ed25519PrivateKey.generate()
    v = verifier(key)
    new = v.verify(sign(release_body(key), key, "approver"), now_ms=START - 10)
    assert new.payload["schema"] == "portfolio_release_v2"
    assert new.payload["mandates"][0]["k"] == "0.025"
    old = v.verify(sign(fixture_release(), key, "approver"), now_ms=20)
    assert old.payload["schema"] == "portfolio_release_v1"
    assert old.payload["batch_seconds"] == 300
    assert new.trading_authorized is False


@pytest.mark.parametrize(
    "fault", ["k", "scope", "account", "policy", "signer", "overlap", "legacy_mix"]
)
def test_new_release_rejects_mandate_scope_drift(tmp_path, fault):
    key = Ed25519PrivateKey.generate()
    body = release_body(key)
    if fault == "k":
        body["mandates"][0]["k"] = "0.05"
    elif fault == "scope":
        body["mandates"][0]["source_scope_generation"] = "f" * 64
    elif fault == "account":
        body["mandates"][0]["account_id"] = "other"
    elif fault == "policy":
        body["mandates"][0]["policy_epoch"] = "f" * 64
    elif fault == "signer":
        body["mandate_authority_sha"] = "f" * 64
    elif fault == "overlap":
        body["mandates"].append(dict(body["mandates"][0], generation=2))
    else:
        body["schema"] = "portfolio_release_v1"
    with pytest.raises(ValueError):
        verifier(key).verify(sign(body, key, "approver"), now_ms=START - 10)


def test_protected_lifecycle_requires_new_generation_to_resume():
    from prop_production.copy_mandates import (
        CopyMandate,
        MandateEvent,
        MandateState,
        transition_mandate,
    )

    m = CopyMandate.parse(mandate_body())
    before = MandateState.initial(m)
    activated = transition_mandate(
        m, before, event=MandateEvent.ACTIVATE, evidence_sha=m.sha, at_ms=START - 1
    )
    baseline = transition_mandate(
        m,
        activated.successor,
        event=MandateEvent.COMPLETE_BASELINE,
        evidence_sha="1" * 64,
        at_ms=START + 100,
        checkpoint_ms=START,
    )
    assert baseline.effect == "BASELINE_ONLY"
    stopped = transition_mandate(
        m, baseline.successor, event=MandateEvent.PAUSE, evidence_sha="2" * 64, at_ms=START + 101
    )
    assert stopped.successor.phase == "PAUSED"
    denied = transition_mandate(
        m, stopped.successor, event=MandateEvent.ACTIVATE, evidence_sha=m.sha, at_ms=START + 102
    )
    assert denied.effect == "REJECTED"
    assert denied.successor.phase == "PAUSED"
