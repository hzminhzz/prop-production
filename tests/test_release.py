import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_signed_release_is_account_bound_and_rollback_safe(tmp_path):
    from prop_production.evidence import digest, sign
    from prop_production.release import ReleaseFloor, ReleaseVerifier, fixture_release

    key = Ed25519PrivateKey.generate()
    payload = fixture_release()
    envelope = sign(payload, key, "release-approver")
    verifier = ReleaseVerifier(key.public_key(), "release-approver", "fixture-account",
                               payload["risk_policy_sha"], payload["rulebook_sha"])
    floor = ReleaseFloor(tmp_path / "protected-floor.sqlite", initialize=True)
    verified = verifier.verify(envelope, now_ms=1000, floor=floor)
    assert verified.release_sha == digest(payload)
    assert verified.trading_authorized is False
    floor.accept(verified)
    later = copy.deepcopy(payload)
    later["generation"] = 2
    floor.accept(verifier.verify(sign(later, key, "release-approver"), now_ms=1000, floor=floor))
    floor.close()
    with ReleaseFloor(tmp_path / "protected-floor.sqlite") as restarted:
        with pytest.raises(ValueError, match="ROLLBACK"):
            verifier.verify(envelope, now_ms=1000, floor=restarted)


@pytest.mark.parametrize("mutation", ["hash", "account", "expired", "schema", "member", "post_seal"])
def test_release_tampering_and_bad_provenance_fail_closed(mutation):
    from prop_production.evidence import digest, sign
    from prop_production.release import ReleaseVerifier, fixture_release

    key = Ed25519PrivateKey.generate()
    original = fixture_release()
    body = copy.deepcopy(original)
    if mutation == "account":
        body["account_binding"] = "other-account"
    elif mutation == "expired":
        body["expires_at_ms"] = 999
    elif mutation == "schema":
        body["schema"] = "unknown"
    elif mutation == "member":
        body["members"] = []
    elif mutation == "post_seal":
        body["members"][0]["identity_known_at_ms"] = 2000
    envelope = sign(body, key, "release-approver")
    if mutation == "hash":
        envelope["payload"]["members"][0]["allocation"] = "999999"
        envelope["sha256"] = digest(envelope["payload"])
    verifier = ReleaseVerifier(key.public_key(), "release-approver", "fixture-account",
                               original["risk_policy_sha"], original["rulebook_sha"])
    with pytest.raises(ValueError):
        verifier.verify(envelope, now_ms=1000)


def test_wrong_signer_and_unknown_decimal_are_rejected():
    from prop_production.evidence import sign
    from prop_production.release import ReleaseVerifier, fixture_release

    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    p = fixture_release()
    verifier = ReleaseVerifier(key.public_key(), "release-approver", "fixture-account",
                               p["risk_policy_sha"], p["rulebook_sha"])
    with pytest.raises(ValueError):
        verifier.verify(sign(p, other, "release-approver"), now_ms=1000)
    p["members"][0]["allocation"] = "NaN"
    with pytest.raises(ValueError):
        verifier.verify(sign(p, key, "release-approver"), now_ms=1000)
