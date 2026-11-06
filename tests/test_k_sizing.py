"""NAV-optional, signed fixed-k sizing while preserving account Guardian control."""

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_planner import book, quote

from prop_production.evidence import digest, sign
from prop_production.planner import Planner
from prop_production.release import ReleaseVerifier, fixture_release


def fixed_k_release(key, *, k="0.04"):
    doc = fixture_release()
    doc["members"][0]["sizing_policy"] = "FIXED_K_NATIVE_QUANTITY"
    doc["members"][0]["k"] = k
    doc["definition_sha"] = digest(doc["members"])
    doc["source_sha"] = digest(
        {
            name: doc[name]
            for name in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    verified = ReleaseVerifier(
        key.public_key(), "approver", "fixture-account", doc["risk_policy_sha"], doc["rulebook_sha"]
    ).verify(sign(doc, key, "approver"), now_ms=1000)
    return doc, verified


def test_fixed_k_no_source_nav_required_and_replays_signed_requests(tmp_path):
    key = Ed25519PrivateKey.generate()
    _, release = fixed_k_release(key, k="0.04")
    planner = Planner(tmp_path / "planner.sqlite", release, key.public_key())
    try:
        source = book(qty="10")
        source["nav"] = None
        planner.ingest(sign(source, key, "source-observer"), local_known_ms=1000)
        proposal = planner.propose(301000, 1, quote())
        pod = proposal.data["pods"][0]
        assert pod["sizing_policy"] == "FIXED_K_NATIVE_QUANTITY"
        assert pod["source_nav"] is None
        # 10 BTC x 10,000 USD mark x signed 0.04 k = $4,000 intent.
        assert pod["requested_notionals"]["BTC"] == "4000.00"
        from prop_production.planner import compile_pods

        assert (
            compile_pods(proposal.data["source_receipts"], release, key.public_key(), 301000)
            == proposal.data["pods"]
        )
    finally:
        planner.close()


@pytest.mark.parametrize("k", ["0", "-0.01", "NaN", "11", "Infinity", ""])
def test_bad_fixed_k_never_accepted_in_signed_release(k):
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError):
        fixed_k_release(key, k=k)


def test_unsigned_k_change_rejected_and_missing_k_does_not_fallback():
    key = Ed25519PrivateKey.generate()
    _, release = fixed_k_release(key)
    doc = release.payload
    from prop_production.evidence import verify

    raw = sign(doc, key, "approver")
    raw["payload"]["members"][0]["k"] = "0.8"
    with pytest.raises(ValueError):
        verify(raw, key.public_key(), "approver")
    raw = doc
    del raw["members"][0]["k"]
    raw["definition_sha"] = digest(raw["members"])
    raw["source_sha"] = digest(
        {
            f: raw[f]
            for f in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    with pytest.raises(ValueError, match="INCOMPLETE_MEMBER"):
        ReleaseVerifier(
            key.public_key(),
            "approver",
            "fixture-account",
            raw["risk_policy_sha"],
            raw["rulebook_sha"],
        ).verify(sign(raw, key, "approver"), now_ms=1000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_complete", False),
        ("book_complete", False),
        ("scope", ["fixture-leader", "unknown-child"]),
        ("source_mode", "RETROSPECTIVE"),
    ],
)
def test_fixed_k_does_not_bypass_scoped_book_or_first_known(tmp_path, field, value):
    key = Ed25519PrivateKey.generate()
    _, release = fixed_k_release(key)
    planner = Planner(tmp_path / "planner.sqlite", release, key.public_key())
    try:
        source = book()
        source["nav"] = None
        source[field] = value
        with pytest.raises(ValueError):
            planner.ingest(sign(source, key, "source-observer"), local_known_ms=1000)
    finally:
        planner.close()


def test_fixed_k_treats_valid_present_nav_as_optional_diagnostic(tmp_path):
    key = Ed25519PrivateKey.generate()
    _, release = fixed_k_release(key)
    planner = Planner(tmp_path / "planner.sqlite", release, key.public_key())
    try:
        planner.ingest(sign(book(), key, "source-observer"), local_known_ms=1000)
        proposal = planner.propose(301000, 1, quote())
        assert proposal.data["pods"][0]["requested_notionals"]["BTC"] == "6000.00"
        assert proposal.data["pods"][0]["source_nav"] == "10000"
    finally:
        planner.close()


def test_legacy_equity_normalized_mode_still_rejects_null_nav(tmp_path):
    key = Ed25519PrivateKey.generate()
    doc = fixture_release()
    release = ReleaseVerifier(
        key.public_key(), "approver", "fixture-account", doc["risk_policy_sha"], doc["rulebook_sha"]
    ).verify(sign(doc, key, "approver"), now_ms=1000)
    planner = Planner(tmp_path / "planner.sqlite", release, key.public_key())
    try:
        source = book()
        source["nav"] = None
        with pytest.raises(ValueError):
            planner.ingest(sign(source, key, "source-observer"), local_known_ms=1000)
    finally:
        planner.close()
