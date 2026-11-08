"""One selected leader may be independently copied into two signed pods."""

import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prop_production.evidence import digest, sign
from prop_production.planner import compile_pods
from prop_production.release import ReleaseVerifier, fixture_release


def approved(body, key):
    return ReleaseVerifier(
        key.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        body["rulebook_sha"],
    ).verify(sign(body, key, "approver"), now_ms=1000)


def signed_shared_release():
    key = Ed25519PrivateKey.generate()
    body = fixture_release()
    first = body["members"][0]
    first["sizing_policy"] = "FIXED_K_NATIVE_QUANTITY"
    first["k"] = "0.1"
    second = copy.deepcopy(first)
    second["pod_id"] = "pod-b"
    second["k"] = "0.2"
    body["members"] = [first, second]
    body["definition_sha"] = digest(body["members"])
    body["source_sha"] = digest(
        {
            key: body[key]
            for key in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    return body, key


def test_one_signed_source_fans_out_fixed_k_to_distinct_pods():
    body, key = signed_shared_release()
    verified = approved(body, key)
    source = Ed25519PrivateKey.generate()
    book = {
        "schema": "source_book_v1",
        "source_identity": "fixture-leader",
        "source_event_ms": 999,
        "known_at_ms": 1000,
        "sequence": 1,
        "scope": ["fixture-leader"],
        "scope_complete": True,
        "book_complete": True,
        "nav": None,
        "source_mode": "SYNTHETIC",
        "positions": [
            {"market": "BTC", "quantity": "2", "mark": "10000", "mark_known_at_ms": 1000}
        ],
    }
    pods = compile_pods(
        [{"envelope": sign(book, source, "source-observer"), "local_known_ms": 1000}],
        verified,
        source.public_key(),
        1000,
    )
    assert [p["pod_id"] for p in pods] == ["pod-a", "pod-b"]
    assert [p["requested_notionals"]["BTC"] for p in pods] == ["2000.0", "4000.0"]


def test_shared_identity_with_conflicting_scope_fails_signed_release_verification():
    body, key = signed_shared_release()
    body["members"][1]["account_scope"] = ["fixture-leader", "extra"]
    body["definition_sha"] = digest(body["members"])
    body["source_sha"] = digest(
        {
            field: body[field]
            for field in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    with pytest.raises(ValueError, match="SHARED_SOURCE_SCOPE_CONFLICT"):
        approved(body, key)
