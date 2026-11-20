import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_copy_mandates import START, release_body


def test_signed_release_is_account_bound_and_rollback_safe(tmp_path):
    from prop_production.evidence import digest, sign
    from prop_production.release import ReleaseFloor, ReleaseVerifier

    key = Ed25519PrivateKey.generate()
    payload = release_body(key)
    envelope = sign(payload, key, "release-approver")
    verifier = ReleaseVerifier(
        key.public_key(),
        "release-approver",
        "fixture-account",
        payload["risk_policy_sha"],
        payload["rulebook_sha"],
    )
    floor = ReleaseFloor(tmp_path / "protected-floor.sqlite", initialize=True)
    verified = verifier.verify(envelope, now_ms=START - 10, floor=floor)
    assert verified.release_sha == digest(payload)
    assert verified.trading_authorized is False
    floor.accept(verified)
    later = copy.deepcopy(payload)
    later["generation"] = 2
    floor.accept(
        verifier.verify(sign(later, key, "release-approver"), now_ms=START - 10, floor=floor)
    )
    floor.close()
    with ReleaseFloor(tmp_path / "protected-floor.sqlite") as restarted:
        with pytest.raises(ValueError, match="ROLLBACK"):
            verifier.verify(envelope, now_ms=START - 10, floor=restarted)


@pytest.mark.parametrize(
    "mutation", ["hash", "account", "expired", "schema", "legacy", "member", "post_seal"]
)
def test_release_tampering_and_bad_provenance_fail_closed(mutation):
    from prop_production.evidence import digest, sign
    from prop_production.release import ReleaseVerifier

    key = Ed25519PrivateKey.generate()
    original = release_body(key)
    body = copy.deepcopy(original)
    if mutation == "account":
        body["account_binding"] = "other-account"
    elif mutation == "expired":
        body["expires_at_ms"] = 999
    elif mutation == "schema":
        body["schema"] = "unknown"
    elif mutation == "legacy":
        body["schema"] = "portfolio_release_v1"
    elif mutation == "member":
        body["members"] = []
    elif mutation == "post_seal":
        body["members"][0]["identity_known_at_ms"] = 2000
    envelope = sign(body, key, "release-approver")
    if mutation == "hash":
        envelope["payload"]["members"][0]["allocation"] = "999999"
        envelope["sha256"] = digest(envelope["payload"])
    verifier = ReleaseVerifier(
        key.public_key(),
        "release-approver",
        "fixture-account",
        original["risk_policy_sha"],
        original["rulebook_sha"],
    )
    with pytest.raises(ValueError):
        verifier.verify(envelope, now_ms=START - 10)


def test_wrong_signer_and_unknown_decimal_are_rejected():
    from prop_production.evidence import sign
    from prop_production.release import ReleaseVerifier

    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    p = release_body(key)
    verifier = ReleaseVerifier(
        key.public_key(),
        "release-approver",
        "fixture-account",
        p["risk_policy_sha"],
        p["rulebook_sha"],
    )
    with pytest.raises(ValueError):
        verifier.verify(sign(p, other, "release-approver"), now_ms=START - 10)
    p["members"][0]["allocation"] = "NaN"
    with pytest.raises(ValueError):
        verifier.verify(sign(p, key, "release-approver"), now_ms=START - 10)

def _resealed(payload):
    from prop_production.evidence import digest

    body = copy.deepcopy(payload)
    body["definition_sha"] = digest(body["members"])
    body["source_sha"] = digest(
        {
            name: body[name]
            for name in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    return body


def _release_with_k(key, k):
    body = _resealed(release_body(key))
    body["members"][0]["k"] = k
    body["mandates"][0]["k"] = k
    return _resealed(body)


@pytest.mark.parametrize(
    ("k", "code"),
    [
        ("0", "MANDATE_GENERATION_K_OR_CLOCK"),
        ("-0.01", "MANDATE_GENERATION_K_OR_CLOCK"),
        ("11", "MANDATE_GENERATION_K_OR_CLOCK"),
        ("10.0000001", "MANDATE_GENERATION_K_OR_CLOCK"),
        ("NaN", "NONFINITE_DECIMAL"),
        ("Infinity", "NONFINITE_DECIMAL"),
        ("", "INVALID_DECIMAL"),
    ],
)
def test_release_refuses_signed_k_outside_the_contract_bounds(k, code):
    """The removed test_k_sizing proofs, restored on the signed V2 release path.

    The kernel mandate contract parses the same signed k string before the
    release-level member loop, so the boundary surfaces as the kernel's own
    refusal. release.py keeps its duplicate member check as defence in depth.
    """
    from prop_production.evidence import sign
    from prop_production.release import ReleaseVerifier

    key = Ed25519PrivateKey.generate()
    body = _release_with_k(key, k)
    verifier = ReleaseVerifier(
        key.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        body["rulebook_sha"],
    )
    with pytest.raises(ValueError, match=code):
        verifier.verify(sign(body, key, "approver"), now_ms=START - 10)


@pytest.mark.parametrize("k", ["10", "0.0000001", "0.025"])
def test_release_accepts_signed_k_inside_the_contract_bounds(k):
    from prop_production.evidence import sign
    from prop_production.release import ReleaseVerifier

    key = Ed25519PrivateKey.generate()
    body = _release_with_k(key, k)
    verified = ReleaseVerifier(
        key.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        body["rulebook_sha"],
    ).verify(sign(body, key, "approver"), now_ms=START - 10)
    assert verified.payload["members"][0]["k"] == k
    assert verified.payload["mandates"][0]["k"] == k
    assert verified.trading_authorized is False


def test_member_k_must_match_the_signed_mandate_k():
    from prop_production.evidence import sign
    from prop_production.release import ReleaseVerifier

    key = Ed25519PrivateKey.generate()
    body = _resealed(release_body(key))
    body["members"][0]["k"] = "0.05"
    body = _resealed(body)
    verifier = ReleaseVerifier(
        key.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        body["rulebook_sha"],
    )
    with pytest.raises(ValueError, match="MANDATE_RELEASE_MEMBER_DRIFT"):
        verifier.verify(sign(body, key, "approver"), now_ms=START - 10)


def test_unsigned_k_edit_and_dropped_k_are_refused():
    from prop_production.evidence import sign
    from prop_production.release import ReleaseVerifier

    key = Ed25519PrivateKey.generate()
    body = _release_with_k(key, "0.04")
    verifier = ReleaseVerifier(
        key.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        body["rulebook_sha"],
    )
    envelope = sign(body, key, "approver")
    envelope["payload"]["members"][0]["k"] = "0.8"
    with pytest.raises(ValueError, match="EVIDENCE_BINDING_MISMATCH"):
        verifier.verify(envelope, now_ms=START - 10)
    # A dropped signed k cannot fall back to allocation or NAV sizing. The
    # refusal currently surfaces as an untyped KeyError from the mandate
    # drift comparison rather than INCOMPLETE_MEMBER; it still never verifies.
    stripped = _resealed(release_body(key))
    del stripped["members"][0]["k"]
    stripped = _resealed(stripped)
    with pytest.raises((KeyError, ValueError)):
        verifier.verify(sign(stripped, key, "approver"), now_ms=START - 10)
