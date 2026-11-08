"""Protected source-only receiver: signer, selection, cutoff, restart, no-trade."""

import json
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_v4_timing import IDENTITY, event, setup

from prop_production.evidence import canonical, digest, sign
from prop_production.v4_source_receiver import (
    SourcePacketReceiver,
    consume_once,
)


def fresh_release(tmp_path):
    projection, release, key = setup(tmp_path)
    projection.close()
    return release, key


def packet(key, seq, *, selected=None, kind="OBSERVATION", value=20, known=300):
    chosen = selected or {"pod-a": IDENTITY}
    content = [] if seq == 1 else [event(key, seq, known, value, kind=kind)]
    events = {"pod-a": content}
    heads = {"pod-a": seq}
    body = {
        "schema": "v4_signed_source_export_v1",
        "head_cursors": heads,
        "events": events,
        "trading_authorized": False,
    }
    binding = {
        "schema": "v4_source_batch_binding_v1",
        "selected": chosen,
        "head_cursors": heads,
        "events_sha": digest(events),
        "trading_authorized": False,
    }
    body["batch_envelope"] = sign(binding, key, "source-observer-v4-batch")
    return canonical(body)


def private_packet(inbox, raw, number):
    packet_sha = sha256(raw).hexdigest()
    path = inbox / f"{number:020d}-{packet_sha[:16]}.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_signed_bootstrap_durable_followup_and_duplicate(tmp_path):
    release, key = fresh_release(tmp_path)
    receiver = SourcePacketReceiver(
        tmp_path / "receiver.sqlite",
        release=release,
        source_key=key.public_key(),
    )
    bootstrap = packet(key, 1)
    assert receiver.ingest(bootstrap, now_ms=200)["events"] == 0
    assert receiver.ingest(bootstrap, now_ms=200)["state"] == "DUPLICATE"
    changed = packet(key, 2)
    assert receiver.ingest(changed, now_ms=300)["events"] == 1
    assert receiver.due(now_ms=300299) == []
    due = receiver.due(now_ms=300300)
    assert len(due) == 1 and due[0]["trading_authorized"] is False
    receiver.close()
    reopened = SourcePacketReceiver(
        tmp_path / "receiver.sqlite",
        release=release,
        source_key=key.public_key(),
    )
    assert reopened.ingest(changed, now_ms=300301)["state"] == "DUPLICATE"
    assert reopened.due(now_ms=300301) == []
    reopened.close()


def test_receiver_rejects_tampered_unsigned_and_rebound_selected_scope(tmp_path):
    release, key = fresh_release(tmp_path)
    receiver = SourcePacketReceiver(
        tmp_path / "receiver.sqlite",
        release=release,
        source_key=key.public_key(),
    )
    forged = json.loads(packet(key, 1))
    forged["head_cursors"]["pod-a"] = 999
    with pytest.raises(ValueError, match="BINDING"):
        receiver.ingest(canonical(forged), now_ms=200)
    other = {"pod-a": "0x" + "b" * 40}
    with pytest.raises(ValueError, match="SCOPE"):
        receiver.ingest(packet(key, 1, selected=other), now_ms=200)
    with pytest.raises(ValueError):
        receiver.ingest(
            packet(Ed25519PrivateKey.generate(), 1),
            now_ms=200,
        )
    with pytest.raises(ValueError, match="FIRST_BATCH"):
        receiver.ingest(packet(key, 2), now_ms=300)
    receiver.close()


def test_private_packet_scan_checks_filename_integrity(tmp_path):
    release, key = fresh_release(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    first = private_packet(inbox, packet(key, 1), 1)
    result = consume_once(
        state_db=tmp_path / "protected.sqlite",
        inbox=inbox,
        release=release,
        source_key=key.public_key(),
        now_ms=200,
    )
    assert result["state"] == "SHADOW_SOURCE_ONLY_NO_ORDER_ROUTE"
    assert result["guardian_financial_admission"] == "BLOCKED_NOT_WIRED"
    assert result["order_route"] == "NONE"
    private_packet(inbox, packet(key, 2), 2)
    second = consume_once(
        state_db=tmp_path / "protected.sqlite",
        inbox=inbox,
        release=release,
        source_key=key.public_key(),
        now_ms=300,
    )
    assert second["processed_source_events"] == 1
    assert second["trading_authorized"] is False
    first.write_bytes(b'{"tampered":true}')
    with pytest.raises(ValueError, match="NAME_DIGEST"):
        consume_once(
            state_db=tmp_path / "protected.sqlite",
            inbox=inbox,
            release=release,
            source_key=key.public_key(),
            now_ms=300301,
        )
