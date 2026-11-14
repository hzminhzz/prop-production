"""Independent broker proof, scoped recovery signatures and no auto-arm."""

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_guardian import setup_system
from test_planner import quote

from prop_production.evidence import digest, sign
from prop_production.gateway import BrokerOrder
from prop_production.recovery import require_real_remediation, snapshot_receipt


def request_for(journal, key, proof_sha, *, now=301010, **changes):
    state = journal.snapshot()
    payload = {
        "schema": "guardian_recovery_request_v2",
        "role": "protected_guardian_recovery",
        "account_binding": state["account_binding"],
        "journal_head": journal.head,
        "journal_sequence": journal.sequence,
        "boot_epoch": state["runtime"]["boot_epoch"],
        "release_sha": state.get("active_release_sha"),
        "recovery_key_sha": sha256(key.public_key().public_bytes_raw()).hexdigest(),
        "reason": "BROKER_UNREACHABLE",
        "operator": "fixture-operator",
        "incident_id": "fixture-incident",
        "nonce": "a" * 32,
        "issued_at_ms": now - 1,
        "expires_at_ms": now + 1000,
        "remediation_sha": proof_sha,
        "reset_daily_baseline": False,
    }
    payload.update(changes)
    return sign(payload, key, "guardian-recovery-operator")


def test_protected_recovery_requires_fresh_independent_broker_remediation(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    key = Ed25519PrivateKey.generate()
    journal.halt("BROKER_UNREACHABLE", 301001)
    now = 301010
    snapshot = broker.observe(now)
    sha = digest(snapshot_receipt(snapshot))
    request = request_for(journal, key, sha, now=now)
    earlier = journal.sequence
    journal.recover_authenticated(
        request,
        recovery_key=key.public_key(),
        gateway=broker,
        now_ms=now,
    )
    state = journal.snapshot()
    assert journal.sequence == earlier + 1
    assert not state["halts"]
    assert state["runtime"]["state"] == "DISARMED"
    assert state["runtime"]["permit"] is None
    assert state["runtime"]["last_recovery"]["remediation_sha"] == sha
    assert state["recovery_nonces"]["a" * 32]["remediation_sha"] == sha
    with pytest.raises(ValueError, match="SCOPE_EXPIRY_OR_REPLAY"):
        journal.recover_authenticated(
            request,
            recovery_key=key.public_key(),
            gateway=broker,
            now_ms=now + 1,
        )
    assert journal.verify()["integrity"] == "VERIFIED"
    journal.close()
    broker.close()
    floor.close()


@pytest.mark.parametrize(
    "violation", ["no_remediation", "wrong_account", "revoked", "expired", "forged"]
)
def test_protected_recovery_rejects_signed_only_or_wrong_authority(tmp_path, violation):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    key = Ed25519PrivateKey.generate()
    journal.halt("BROKER_UNREACHABLE", 301001)
    now = 301010
    proof_sha = digest(snapshot_receipt(broker.observe(now)))
    request = request_for(journal, key, proof_sha, now=now)
    revoked = set()
    if violation == "wrong_account":
        request = request_for(journal, key, proof_sha, now=now, account_binding="wrong")
    if violation == "expired":
        request = request_for(journal, key, proof_sha, now=now, expires_at_ms=now)
    if violation == "forged":
        request = request_for(journal, key, "b" * 64, now=now)
    if violation == "revoked":
        revoked.add(sha256(key.public_key().public_bytes_raw()).hexdigest())
    if violation == "no_remediation":
        broker.connected = False
    original = journal.sequence
    with pytest.raises((ValueError, TimeoutError)):
        journal.recover_authenticated(
            request,
            recovery_key=key.public_key(),
            gateway=broker,
            now_ms=now,
            revoked_key_shas=revoked,
        )
    assert journal.sequence == original
    assert "BROKER_UNREACHABLE" in journal.snapshot()["halts"]
    journal.close()
    broker.close()
    floor.close()


def test_guardian_pinned_recovery_authority_owns_financial_clearance(tmp_path):
    from prop_production.guardian import Guardian

    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    recovery_key = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError, match="MUST_BE_DISTINCT"):
        Guardian(
            journal,
            guardian.release,
            floor,
            guardian.source_key,
            guardian.rules,
            guardian.policy,
            guardian.markets,
            broker,
            recovery_key=guardian.source_key,
        )
    secured = Guardian(
        journal,
        guardian.release,
        floor,
        guardian.source_key,
        guardian.rules,
        guardian.policy,
        guardian.markets,
        broker,
        recovery_key=recovery_key.public_key(),
    )
    journal.halt("BROKER_UNREACHABLE", 301001)
    now = 301010
    evidence_sha = digest(snapshot_receipt(broker.observe(now)))
    request = request_for(journal, recovery_key, evidence_sha, now=now)
    secured.recover_authenticated(request, now)
    assert journal.snapshot()["runtime"]["state"] == "DISARMED"
    assert not journal.snapshot()["halts"]
    journal.close()
    broker.close()
    floor.close()


def test_nonfixture_recovery_rejects_unpinned_fresh_operator_key(tmp_path):
    from prop_production.journal import Journal

    key = Ed25519PrivateKey.generate()
    with Journal(tmp_path / "protected.sqlite", "protected-account", initialize=True) as journal:
        journal.halt("BROKER_UNREACHABLE", 100)
        forged = request_for(journal, key, "f" * 64, now=200)
        with pytest.raises(ValueError, match="TRUST_FLOOR_MISSING"):
            journal.recover_authenticated(
                forged, recovery_key=key.public_key(), gateway=None, now_ms=200,
            )
        assert "BROKER_UNREACHABLE" in journal.snapshot()["halts"]


def test_legacy_digest_only_recovery_cannot_clear_real_account(tmp_path):
    from prop_production.journal import Journal

    with Journal(tmp_path / "protected.sqlite", "nonfixture-account", initialize=True) as journal:
        journal.halt("BROKER_UNREACHABLE", 100)
        with pytest.raises(ValueError, match="PROTECTED_RECOVERY_REQUIRES"):
            journal.recover(
                "BROKER_UNREACHABLE",
                200,
                operator="any",
                incident_id="INC",
                evidence_sha="f" * 64,
            )
        assert journal.snapshot()["halts"] == ["BROKER_UNREACHABLE"]


def test_recovery_rejects_revoked_non_broker_order_status(tmp_path):
    guardian, broker, journal, proposal, floor = setup_system(tmp_path)
    key = Ed25519PrivateKey.generate()
    try:
        now = 301000
        decision = guardian.decide(proposal, quote(), now)
        ids = guardian.reserve(decision.data["decision_id"], now)
        intent_id = ids[0]
        journal.halt("BROKER_UNREACHABLE", now + 1)
        state = journal.snapshot()
        intent = state["intents"][intent_id]
        revoked_order = BrokerOrder(
            intent_id=intent_id,
            broker_id="fake-revoked",
            market=intent["market"],
            quantity=Decimal(intent["quantity"]),
            filled=Decimal(0),
            status="REVOKED",
            reduce_only=intent["reduce_only"],
        )
        snapshot = replace(broker.observe(now + 2), orders=(revoked_order,))
        assert snapshot.ready(now + 2, 5000) is True
        with pytest.raises(ValueError, match="RECOVERY_TERMINALITY_NOT_CONSERVED"):
            require_real_remediation(
                reason="BROKER_UNREACHABLE",
                state=state,
                snapshot=snapshot,
                now_ms=now + 2,
            )

        class RevokedSnapshotGateway:
            synthetic = True
            endpoint = snapshot.endpoint

            def observe(self, observe_ms: int):
                return snapshot

        request = request_for(journal, key, digest(snapshot_receipt(snapshot)), now=now + 2)
        with pytest.raises(ValueError, match="RECOVERY_TERMINALITY_NOT_CONSERVED"):
            journal.recover_authenticated(
                request,
                recovery_key=key.public_key(),
                gateway=RevokedSnapshotGateway(),
                now_ms=now + 2,
            )
        assert "BROKER_UNREACHABLE" in journal.snapshot()["halts"]
    finally:
        journal.close()
        broker.close()
        floor.close()
