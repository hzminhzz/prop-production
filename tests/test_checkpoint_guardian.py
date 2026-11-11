"""S2: signed checkpoint/mandate through the existing Guardian and journal."""

import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_checkpoint_receiver import KEY, fixture_packet
from test_copy_mandates import START, release_body

from prop_production.evidence import digest, sign
from prop_production.finance import Rulebook
from prop_production.guardian import Guardian
from prop_production.journal import Journal
from prop_production.judge.broker import FakeBroker
from prop_production.release import ReleaseFloor, ReleaseVerifier
from prop_production.risk import fixture_markets, fixture_policy
from prop_production.v4_source_receiver import SourcePacketReceiver


@pytest.fixture
def system(tmp_path):
    authority = Ed25519PrivateKey.generate()
    rules, policy, markets = Rulebook.synthetic(), fixture_policy(), fixture_markets()
    markets["BTC"]["source_instrument"] = "hyperliquid/mainnet:perp:default/BTC"
    markets["ETH"]["source_instrument"] = "hyperliquid/mainnet:perp:default/ETH"
    body = release_body(authority)
    body["risk_policy_sha"] = digest({"policy": policy, "markets": markets})
    body["rulebook_sha"] = rules.sha
    for m in body["mandates"]:
        m.update(risk_policy_sha=body["risk_policy_sha"], rulebook_sha=rules.sha)
    release = ReleaseVerifier(
        authority.public_key(), "approver", "fixture-account", body["risk_policy_sha"], rules.sha
    ).verify(sign(body, authority, "approver"), now_ms=START - 10)
    floor = ReleaseFloor(tmp_path / "floor.sqlite3", initialize=True)
    floor.accept(release)
    journal = Journal(tmp_path / "guardian.sqlite3", "fixture-account", initialize=True)
    broker = FakeBroker(tmp_path / "broker.sqlite3")
    receiver = None
    try:
        guardian = Guardian(
            journal,
            release,
            floor,
            KEY.public_key(),
            rules,
            policy,
            markets,
            broker,
            mandate_key=authority.public_key(),
        )
        receiver = SourcePacketReceiver(
            tmp_path / "source.sqlite3",
            release=release,
            source_key=KEY.public_key(),
            wire_version=3,
        )
        yield SimpleNamespace(
            guardian=guardian,
            journal=journal,
            broker=broker,
            floor=floor,
            receiver=receiver,
            authority=authority,
            release=release,
            mandate=body["mandates"][0],
            root=tmp_path,
        )
    finally:
        if receiver is not None:
            receiver.close()
        journal.close()
        broker.close()
        floor.close()


def prime(system):
    from prop_production.copy_mandates import CopyMandate

    m = CopyMandate.parse(system.mandate)
    system.guardian.activate_copy_mandate(
        sign(m.data, system.authority, "copy-mandate-v1"), now_ms=START - 1
    )
    now = json.loads(fixture_packet())["payload"]["issued_ms"]
    system.receiver.ingest(fixture_packet(), now_ms=now)
    system.guardian.baseline_copy_mandate(m.owner, system.receiver, now_ms=now)
    return m, now


def test_guardian_mandate_lifecycle_is_durable_and_nonfinancial(system):
    m, now = prime(system)
    state = system.journal.snapshot()
    assert state["copy_mandates"][m.owner]["state"]["phase"] == "ACTIVE"
    assert state["copy_mandates"][m.owner]["history"][-1]["effect"] == "BASELINE_ONLY"
    before = {
        k: state[k]
        for k in (
            "finance",
            "positions",
            "pod_positions",
            "intents",
            "fills",
            "reservations",
            "halts",
        )
    }
    command = sign(
        {
            "schema": "copy_mandate_control_v1",
            "owner": m.owner,
            "mandate_sha": m.sha,
            "predecessor_sha": digest(state["copy_mandates"][m.owner]["state"]),
            "event": "PAUSE",
            "issued_ms": now,
            "expires_ms": now + 1000,
            "nonce": digest("pause"),
        },
        system.authority,
        "copy-mandate-control-v1",
    )
    result = system.guardian.control_copy_mandate(command, now_ms=now + 1)
    assert result.phase == "PAUSED"
    assert system.guardian.control_copy_mandate(command, now_ms=now + 2) == result
    assert {k: system.journal.snapshot()[k] for k in before} == before
    assert system.broker.audit() == []
    system.journal.verify()


def test_source_signer_cannot_activate_a_copy_mandate(system):
    with pytest.raises(ValueError):
        system.guardian.activate_copy_mandate(
            sign(system.mandate, KEY, "copy-mandate-v1"), now_ms=START - 1
        )
    assert not system.journal.snapshot().get("copy_mandates")
    assert system.broker.audit() == []


def test_journal_oracle_rejects_implicit_mandate_activation(system):
    m, now = prime(system)
    state = system.journal.snapshot()
    row = state["copy_mandates"][m.owner]
    row["state"]["baseline_outcome_id"] = "f" * 64
    with pytest.raises(ValueError, match="MANDATE_STATE_REPLAY"):
        system.journal.commit("FORGED_STATE", now, state, expected_sequence=system.journal.sequence)
    assert system.broker.audit() == []


def test_checkpoint_release_cannot_use_legacy_admission(system):
    with pytest.raises(ValueError, match="CHECKPOINT_ADMISSION"):
        system.guardian.decide(None, {}, START)
    assert not system.journal.snapshot()["intents"]
