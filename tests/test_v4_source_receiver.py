"""Protected source-only receiver: signer, selection, cutoff, restart, no-trade."""

import json
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_v4_timing import IDENTITY, event, setup

from prop_production.evidence import canonical, digest, sign, verify
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


def v2_packet(key, *, anchor=None, seq=2, previous=None):
    selected = {"pod-a": IDENTITY}
    epoch = "e" * 32
    if anchor is None:
        anchor = sign(
            {
                "schema": "v4_source_stream_head_anchor_v2",
                "selected": selected,
                "heads": {"pod-a": {"cursor": 1, "chain_sha": digest(["chain", 1])}},
                "wire_epoch": epoch,
                "cutoff_ms": 200,
                "trading_authorized": False,
            },
            key,
            "source-observer-v4-anchor",
        )
        events = {"pod-a": []}
    else:
        record = event(key, seq, 300 if seq == 2 else 350, 20)
        proof = verify(record["proof"], key.public_key(), "source-observer-v4")
        proof.update(
            schema="v4_official_event_proof_v2",
            wire_epoch=epoch,
            anchor_sha=digest(anchor["payload"]),
            previous_stream_chain_sha=(
                previous if previous is not None else digest(["chain", seq - 1])
            ),
        )
        record["proof"] = sign(proof, key, "source-observer-v4")
        events = {"pod-a": [record]}
    heads = {"pod-a": 1 if not events["pod-a"] else seq}
    body = {
        "schema": "v4_signed_source_export_v2",
        "head_cursors": heads,
        "events": events,
        "trading_authorized": False,
        "anchor": anchor,
    }
    body["batch_envelope"] = sign(
        {
            "schema": "v4_source_batch_binding_v2",
            "selected": selected,
            "head_cursors": heads,
            "events_sha": digest(events),
            "wire_epoch": epoch,
            "anchor_sha": digest(anchor["payload"]),
            "trading_authorized": False,
        },
        key,
        "source-observer-v4-batch",
    )
    return canonical(body)


def test_v2_packet_quarantines_only_attributable_wallet_failure(tmp_path):
    from prop_production.release import ReleaseVerifier, fixture_release

    approver, source = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    quarantine_signer = Ed25519PrivateKey.generate()
    identity_b = "0x" + "b" * 40
    body = fixture_release()
    body["source_mode"] = "PROSPECTIVE"
    primary = body["members"][0]
    primary.update(
        pod_id="pod-a",
        source_identity=IDENTITY,
        account_scope=[IDENTITY],
        sizing_policy="FIXED_K_NATIVE_QUANTITY",
        allowed_markets=["hyperliquid/mainnet:perp:default/BTC"],
        k="0.025",
    )
    body["members"] = [
        primary,
        {
            **primary,
            "pod_id": "pod-b",
            "source_identity": identity_b,
            "account_scope": [identity_b],
        },
    ]
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
    release = ReleaseVerifier(
        approver.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        body["rulebook_sha"],
    ).verify(sign(body, approver, "approver"), now_ms=1000)
    selected = {"pod-a": IDENTITY, "pod-b": identity_b}
    epoch = "e" * 32
    chain0 = digest(["chain", 1])
    anchor = sign(
        {
            "schema": "v4_source_stream_head_anchor_v2",
            "selected": selected,
            "heads": {alias: {"cursor": 1, "chain_sha": chain0} for alias in selected},
            "wire_epoch": epoch,
            "cutoff_ms": 200,
            "trading_authorized": False,
        },
        source,
        "source-observer-v4-anchor",
    )

    def batch(events, heads):
        packet_body = {
            "schema": "v4_signed_source_export_v2",
            "events": events,
            "head_cursors": heads,
            "anchor": anchor,
            "trading_authorized": False,
        }
        packet_body["batch_envelope"] = sign(
            {
                "schema": "v4_source_batch_binding_v2",
                "selected": selected,
                "head_cursors": heads,
                "events_sha": digest(events),
                "anchor_sha": digest(anchor["payload"]),
                "wire_epoch": epoch,
                "trading_authorized": False,
            },
            source,
            "source-observer-v4-batch",
        )
        return canonical(packet_body)

    def signed_event(alias, identity, *, bad=False, seq=2, kind="OBSERVATION"):
        record = event(
            source,
            seq,
            300 if seq == 2 else 350,
            20,
            kind=kind,
            identity=identity,
            scope=[identity],
        )
        proof = verify(record["proof"], source.public_key(), "source-observer-v4")
        proof.update(
            schema="v4_official_event_proof_v2",
            stream_id="account:" + alias,
            anchor_sha=digest(anchor["payload"]),
            wire_epoch=epoch,
            previous_stream_chain_sha="f" * 64 if bad else digest(["chain", seq - 1]),
        )
        record["proof"] = sign(proof, source, "source-observer-v4")
        return record

    receiver = SourcePacketReceiver(
        tmp_path / "multi.sqlite",
        release=release,
        source_key=source.public_key(),
        wire_version=2,
        quarantine_key=quarantine_signer.public_key(),
    )
    assert (
        receiver.ingest(batch({"pod-a": [], "pod-b": []}, {"pod-a": 1, "pod-b": 1}), now_ms=200)[
            "state"
        ]
        == "COMPLETE"
    )
    mixed = batch(
        {
            "pod-a": [signed_event("pod-a", IDENTITY)],
            "pod-b": [signed_event("pod-b", identity_b, bad=True)],
        },
        {"pod-a": 2, "pod-b": 2},
    )
    partial = receiver.ingest(mixed, now_ms=300)
    assert partial["events"] == 1
    assert partial["state"] == "PARTIAL" and partial["quarantined_events"] == 1
    cursors = dict(receiver.projector.con.execute("SELECT identity,cursor FROM streams"))
    assert cursors[IDENTITY] == 2 and cursors[identity_b] == 1
    packet_sha = sha256(mixed).hexdigest()
    assert dict(
        receiver.projector.con.execute(
            "SELECT alias,status FROM bridge_packet_events WHERE packet_sha=?", (packet_sha,)
        )
    ) == {"pod-a": "ACCEPTED", "pod-b": "QUARANTINED"}
    assert receiver.ingest(mixed, now_ms=301)["state"] == "PARTIAL"
    receiver.close()
    restarted = SourcePacketReceiver(
        tmp_path / "multi.sqlite",
        release=release,
        source_key=source.public_key(),
        wire_version=2,
        quarantine_key=quarantine_signer.public_key(),
    )
    genuine_baseline = signed_event("pod-b", identity_b, seq=2, kind="BASELINE")
    resolution = {
        "schema": "v4_wallet_quarantine_resolution_v1",
        "packet_sha": packet_sha,
        "alias": "pod-b",
        "source_identity": identity_b,
        "account_binding": "fixture-account",
        "release_sha": release.release_sha,
        "anchor_sha": digest(anchor["payload"]),
        "quarantined_event_sha": dict(
            restarted.projector.con.execute(
                "SELECT alias,event_sha FROM bridge_packet_events "
                "WHERE packet_sha=? AND status='QUARANTINED'",
                (packet_sha,),
            )
        )["pod-b"],
        "baseline_sha": digest(genuine_baseline),
        "expected_stream_head": chain0,
        "reason_code": "SOURCE_EVENT_QUARANTINED",
        "role": "source_quarantine_operator",
        "nonce": "a" * 32,
        "issued_at_ms": 300,
        "expires_at_ms": 600,
    }
    signed_resolution = sign(resolution, quarantine_signer, "v4-quarantine-operator")
    assert (
        restarted.resolve_quarantine(
            packet_sha=packet_sha,
            alias="pod-b",
            baseline=genuine_baseline,
            disposition=signed_resolution,
            now_ms=301,
        )["state"]
        == "RESOLVED_WITH_QUARANTINE"
    )
    assert restarted.ingest(mixed, now_ms=302)["state"] == "RESOLVED_WITH_QUARANTINE"
    with pytest.raises(ValueError, match="CURSOR"):
        restarted.resolve_quarantine(
            packet_sha=packet_sha,
            alias="pod-b",
            baseline=genuine_baseline,
            disposition=signed_resolution,
            now_ms=302,
        )
    healthy_b = batch(
        {
            "pod-a": [],
            "pod-b": [signed_event("pod-b", identity_b, seq=3)],
        },
        {"pod-a": 2, "pod-b": 3},
    )
    assert restarted.ingest(healthy_b, now_ms=351)["state"] == "COMPLETE"
    original = restarted.projector.con.execute(
        "SELECT status FROM bridge_packet_status WHERE packet_sha=?", (packet_sha,)
    ).fetchone()
    assert original[0] == "RESOLVED_WITH_QUARANTINE"
    assert (
        restarted.projector.con.execute(
            "SELECT status FROM bridge_packet_events WHERE packet_sha=? AND alias=?",
            (packet_sha, "pod-b"),
        ).fetchone()[0]
        == "QUARANTINED"
    )
    restarted.close()


def test_signed_v2_anchor_predecessor_and_no_v1_downgrade(tmp_path):
    release, key = fresh_release(tmp_path)
    receiver = SourcePacketReceiver(
        tmp_path / "v2.sqlite", release=release, source_key=key.public_key(), wire_version=2
    )
    raw_baseline = v2_packet(key)
    assert receiver.ingest(raw_baseline, now_ms=200)["state"] == "COMPLETE"
    assert receiver.ingest(raw_baseline, now_ms=200)["state"] == "DUPLICATE"
    anchor = json.loads(raw_baseline)["anchor"]
    good = v2_packet(key, anchor=anchor)
    assert receiver.ingest(good, now_ms=300)["state"] == "COMPLETE"
    assert receiver.ingest(good, now_ms=301)["state"] == "DUPLICATE"
    wrong = v2_packet(key, anchor=anchor, seq=3, previous="f" * 64)
    result = receiver.ingest(wrong, now_ms=350)
    assert result["state"] == "PARTIAL"
    assert result["quarantined_events"] == 1
    assert (
        receiver.projector.con.execute(
            "SELECT cursor FROM streams WHERE identity=?", (IDENTITY,)
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(ValueError, match="DOWNGRADE"):
        receiver.ingest(packet(key, 3), now_ms=350)
    receiver.close()
    restarted = SourcePacketReceiver(
        tmp_path / "v2.sqlite", release=release, source_key=key.public_key(), wire_version=2
    )
    assert restarted.ingest(wrong, now_ms=351)["state"] == "PARTIAL"
    assert restarted.ingest(v2_packet(key, anchor=anchor, seq=3), now_ms=351)["state"] == "COMPLETE"
    restarted.close()


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
