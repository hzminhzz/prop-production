"""New signed checkpoint path in the existing protected source receiver."""

import json
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_v4_source_receiver import fresh_release

from prop_production.evidence import canonical, digest, sign
from prop_production.v4_source_receiver import SourcePacketReceiver

KEY = Ed25519PrivateKey.from_private_bytes(b"c" * 32)
POLICY = {
    "name": "cwsa_utc_5m_v3",
    "limits": {
        "max_state_age_ms": 4000,
        "max_acquisition_ms": 3000,
        "max_dex_skew_ms": 2000,
        "publication_grace_ms": 5000,
    },
    "measurement_sha": "c" * 64,
}
SCOPE = {
    "schema_version": 3,
    "source_id": "fixture-leader",
    "account_sha": sha256(("0x" + "a" * 40).encode()).hexdigest(),
    "generation": "a" * 64,
    "selection_sha": "b" * 64,
    "dexes": ["default", "xyz"],
}
PINS = {"policy": POLICY, "scopes": {"fixture-leader": SCOPE}}


def test_existing_receiver_supports_separately_pinned_checkpoint_schema(tmp_path):
    release, _key = fresh_release(tmp_path)
    receiver = SourcePacketReceiver(
        tmp_path / "checkpoint.sqlite3",
        release=release,
        source_key=KEY.public_key(),
        wire_version=3,
        checkpoint_pins=PINS,
    )
    assert receiver.checkpoint_status()["source_count"] == 0
    assert receiver.due(now_ms=1) == []
    receiver.close()


def test_partial_and_forged_checkpoint_packets_do_not_advance(tmp_path):
    release, _key = fresh_release(tmp_path)
    receiver = SourcePacketReceiver(
        tmp_path / "checkpoint.sqlite3",
        release=release,
        source_key=KEY.public_key(),
        wire_version=3,
        checkpoint_pins=PINS,
    )
    with pytest.raises((ValueError, KeyError)):
        receiver.ingest(b'{"payload":', now_ms=1)
    body = {
        "schema": "cwsa_checkpoint_packet_v3",
        "scope": SCOPE,
        "policy": POLICY,
        "transitions": [],
        "originals": {},
        "issued_ms": 0,
        "trading_authorized": False,
    }
    raw = canonical(sign(body, Ed25519PrivateKey.generate(), "cwsa-checkpoint-v3"))
    with pytest.raises(ValueError):
        receiver.ingest(raw, now_ms=1)
    assert receiver.checkpoint_status()["source_count"] == 0
    receiver.close()


def fixture_packet():
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / "cwsa_checkpoint_packet_v3.json").read_bytes()


def test_two_independent_consumers_preserve_one_cwsa_change_and_restart(tmp_path):
    release, _key = fresh_release(tmp_path)
    body = json.loads(fixture_packet())["payload"]
    now = body["issued_ms"]
    expected = body["transitions"][-1]["effect"]["signal_id"]
    for name in ("paper", "shadow"):
        receiver = SourcePacketReceiver(
            tmp_path / (name + ".sqlite3"),
            release=release,
            source_key=KEY.public_key(),
            wire_version=3,
            checkpoint_pins=PINS,
        )
        assert receiver.ingest(fixture_packet(), now_ms=now)["events"] == 3
        outcomes = receiver.checkpoint_outcomes(now_ms=now)
        assert [item["opening_candidate"] for item in outcomes] == [False, True]
        assert outcomes[-1]["outcome"]["signal_id"] == expected
        assert receiver.due(now_ms=now + 300_000) == []
        receiver.close()
        receiver = SourcePacketReceiver(
            tmp_path / (name + ".sqlite3"),
            release=release,
            source_key=KEY.public_key(),
            wire_version=3,
            checkpoint_pins=PINS,
        )
        assert receiver.ingest(fixture_packet(), now_ms=now + 1)["state"] == "DUPLICATE"
        assert receiver.checkpoint_status()["sources"]["fixture-leader"]["cursor"] == 3
        assert not any(
            item["opening_candidate"] for item in receiver.checkpoint_outcomes(now_ms=now + 300_000)
        )
        receiver.close()


@pytest.mark.parametrize(
    "fault", ["request", "vector", "predecessor", "future", "scope", "raw", "partial"]
)
def test_signed_but_inconsistent_proofs_cannot_advance(tmp_path, fault):
    release, _key = fresh_release(tmp_path)
    receiver = SourcePacketReceiver(
        tmp_path / "negative.sqlite3",
        release=release,
        source_key=KEY.public_key(),
        wire_version=3,
        checkpoint_pins=PINS,
    )
    body = json.loads(fixture_packet())["payload"]
    now = body["issued_ms"]
    event = body["transitions"][-1]
    if fault == "request":
        event["effect"]["sample"]["receipts"][0]["request_sha"] = "f" * 64
        event["evidence_sha"] = digest(event["effect"]["sample"])
    elif fault == "vector":
        event["effect"]["positions"] = []
    elif fault == "predecessor":
        event["prior_sha"] = "f" * 64
    elif fault == "future":
        body["issued_ms"] = now + 1
    elif fault == "scope":
        body["scope"]["generation"] = "f" * 64
    elif fault == "raw":
        body["originals"].pop(next(iter(body["originals"])))
    else:
        body["transitions"] = body["transitions"][1:]
    with pytest.raises((ValueError, KeyError)):
        receiver.ingest(canonical(sign(body, KEY, "cwsa-checkpoint-v3")), now_ms=now)
    assert receiver.checkpoint_status()["source_count"] == 0
    receiver.close()


def test_later_source_fence_revokes_unconsumed_opening_candidate(tmp_path):
    release, _key = fresh_release(tmp_path)
    receiver = SourcePacketReceiver(
        tmp_path / "fence.sqlite3",
        release=release,
        source_key=KEY.public_key(),
        wire_version=3,
        checkpoint_pins=PINS,
    )
    body = json.loads(fixture_packet())["payload"]
    now = body["issued_ms"]
    receiver.ingest(fixture_packet(), now_ms=now)
    previous = body["transitions"][-1]
    before = previous["successor"]
    event = dict(
        previous,
        sequence=4,
        prior_sha=digest(previous),
        predecessor=before,
        event="CONFLICT",
        evidence_sha="e" * 64,
        guard="EQUIVOCATION",
        at_ms=now,
        successor=dict(before, sequence=4, phase="FENCED"),
        effect=None,
    )
    body["transitions"] = [event]
    receiver.ingest(canonical(sign(body, KEY, "cwsa-checkpoint-v3")), now_ms=now)
    assert not any(item["opening_candidate"] for item in receiver.checkpoint_outcomes(now_ms=now))
    receiver.close()


def test_existing_handoff_and_inbox_keep_checkpoint_proof_bytes(tmp_path):
    from test_v4_source_handoff import copy_once, directories
    from test_v4_source_receiver import private_packet

    from prop_production.v4_source_receiver import consume_once

    release, _key = fresh_release(tmp_path)
    src, dst = directories(tmp_path)
    private_packet(src, fixture_packet(), 1)
    assert copy_once(src, dst, KEY)["transferred"] == 1
    now = json.loads(fixture_packet())["payload"]["issued_ms"]
    result = consume_once(
        state_db=tmp_path / "inbox.sqlite3",
        inbox=dst,
        release=release,
        source_key=KEY.public_key(),
        now_ms=now,
        wire_version=3,
        checkpoint_pins=PINS,
    )
    assert result["processed_source_events"] == 3
    assert result["opening_candidates"] == 1
    assert result["order_route"] == "NONE"


def crash_receiver(root, release_body, point):
    import os
    import signal
    from pathlib import Path

    from prop_production.release import VerifiedRelease

    payload = json.loads(release_body)
    release = VerifiedRelease(canonical(payload), digest(payload), "approver")
    receiver = SourcePacketReceiver(
        Path(root) / "crash.sqlite3",
        release=release,
        source_key=KEY.public_key(),
        wire_version=3,
        checkpoint_pins=PINS,
    )
    if point == "before_commit":

        def trace(sql):
            if sql == "COMMIT":
                os.kill(os.getpid(), signal.SIGKILL)

        receiver.checkpoint.con.set_trace_callback(trace)
    now = json.loads(fixture_packet())["payload"]["issued_ms"]
    receiver.ingest(fixture_packet(), now_ms=now)
    os.kill(os.getpid(), signal.SIGKILL)


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_receiver_sigkill_never_commits_partial_source_packet(tmp_path, point):
    import os
    import signal
    import subprocess
    import sys

    release, _key = fresh_release(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from test_checkpoint_receiver import crash_receiver;import sys;crash_receiver(*sys.argv[1:])",
            str(tmp_path),
            json.dumps(release.payload),
            point,
        ],
        env=os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=15,
    )
    assert result.returncode == -signal.SIGKILL
    receiver = SourcePacketReceiver(
        tmp_path / "crash.sqlite3",
        release=release,
        source_key=KEY.public_key(),
        wire_version=3,
        checkpoint_pins=PINS,
    )
    assert receiver.checkpoint_status()["source_count"] == (0 if point == "before_commit" else 1)
    now = json.loads(fixture_packet())["payload"]["issued_ms"]
    receiver.ingest(fixture_packet(), now_ms=now)
    assert receiver.checkpoint_status()["sources"]["fixture-leader"]["cursor"] == 3
    receiver.close()
