import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def setup_planner(tmp_path):
    from prop_production.evidence import sign
    from prop_production.planner import Planner
    from prop_production.release import ReleaseVerifier, fixture_release
    key = Ed25519PrivateKey.generate()
    release = fixture_release()
    verified = ReleaseVerifier(key.public_key(), "approver", "fixture-account",
                               release["risk_policy_sha"], release["rulebook_sha"]).verify(
                                   sign(release, key, "approver"), now_ms=1000)
    return Planner(tmp_path / "planner.sqlite", verified, key.public_key()), key, verified


def book(seq=1, known=1000, qty="15"):
    return {"schema": "source_book_v1", "source_identity": "fixture-leader",
            "source_event_ms": known - 1, "known_at_ms": known, "sequence": seq,
            "scope": ["fixture-leader"], "scope_complete": True, "book_complete": True,
            "nav": "10000", "positions": [{"market": "BTC", "quantity": qty,
             "mark": "10000", "mark_known_at_ms": known}], "source_mode": "SYNTHETIC"}


def quote(known=301000):
    return {"BTC": {"bid": "10000", "ask": "10030", "known_at_ms": known}}


def test_first_receipt_anchors_nonresetting_300s_and_15x_is_only_request(tmp_path):
    from prop_production.evidence import sign
    from prop_production.planner import Planner
    planner, key, release = setup_planner(tmp_path)
    planner.ingest(sign(book(), key, "source-observer"), local_known_ms=1000)
    planner.ingest(sign(book(2, 200000, "10"), key, "source-observer"), local_known_ms=200000)
    assert planner.propose(300999, 1, quote()) is None
    proposal = planner.propose(301000, 1, quote())
    assert proposal.data["due_ms"] == 301000
    assert proposal.data["pods"][0]["requested_notionals"]["BTC"] == "20000"
    assert proposal.data["first_known_ms"] == 1000
    planner.close()
    resumed = Planner(tmp_path / "planner.sqlite", release, key.public_key())
    assert resumed.propose(301000, 1, quote()).payload == proposal.payload
    resumed.close()


@pytest.mark.parametrize("field,value", [
    ("scope_complete", False), ("nav", "0"), ("source_event_ms", 1001),
    ("scope", ["missing-subaccount"]), ("source_mode", "RETROSPECTIVE"),
])
def test_bad_source_book_cannot_propose(tmp_path, field, value):
    from prop_production.evidence import sign
    planner, key, _ = setup_planner(tmp_path)
    body = book()
    body[field] = value
    with pytest.raises(ValueError):
        planner.ingest(sign(body, key, "source-observer"), local_known_ms=1000)
    assert planner.propose(301000, 1, quote()) is None
    planner.close()


def test_stale_quote_and_conflicting_receipt_do_not_open(tmp_path):
    from prop_production.evidence import sign
    planner, key, _ = setup_planner(tmp_path)
    planner.ingest(sign(book(), key, "source-observer"), local_known_ms=1000)
    with pytest.raises(ValueError, match="QUOTE"):
        planner.propose(301000, 1, quote(1000))
    bad = copy.deepcopy(book())
    bad["positions"][0]["quantity"] = "9"
    with pytest.raises(ValueError, match="SEQUENCE"):
        planner.ingest(sign(bad, key, "source-observer"), local_known_ms=1000)
    planner.close()
