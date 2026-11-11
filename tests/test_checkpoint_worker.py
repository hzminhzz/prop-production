"""Nonactivating rehearsal uses the existing isolated Guardian process and IPC."""

import base64
import json
import os
from copy import deepcopy

import pytest
from test_checkpoint_receiver import KEY, POLICY, SCOPE, fixture_packet
from test_copy_mandates import START, release_body

from prop_production.copy_mandates import CopyMandate
from prop_production.evidence import canonical, digest, sign
from prop_production.ipc import LocalClient
from prop_production.journal import Journal
from prop_production.judge.runner import Worker
from prop_production.release import ReleaseFloor, ReleaseVerifier
from prop_production.worker import initialize_fixture, public_fixture_key


def setup(root):
    initialize_fixture(root)
    authority = public_fixture_key(17, "approver")
    settings = json.loads((root / "trust/settings.json").read_bytes())
    policy, markets = settings["policy"], settings["markets"]
    policy["checkpoint_execution"] = {
        "quote_max_age_ms": 2000,
        "account_max_age_ms": 2000,
        "max_gross_notional": "20000",
    }
    for name, spec in markets.items():
        spec["source_instrument"] = "hyperliquid/mainnet:perp:default/" + name
    body = release_body(authority)
    body.update(
        generation=2,
        risk_policy_sha=digest({"policy": policy, "markets": markets}),
        rulebook_sha=digest(settings["rules"]),
    )
    # Rulebook.sha includes its canonical dataclass fields.
    from prop_production.finance import Rulebook

    body["rulebook_sha"] = Rulebook(**settings["rules"]).sha
    for mandate in body["mandates"]:
        mandate.update(risk_policy_sha=body["risk_policy_sha"], rulebook_sha=body["rulebook_sha"])
    settings.update(
        release=sign(body, authority, "approver"),
        verification_ms=START - 10,
        source_public=base64.b64encode(KEY.public_key().public_bytes_raw()).decode(),
    )
    (root / "trust/settings.json").write_bytes(canonical(settings))
    release = ReleaseVerifier(
        authority.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        body["rulebook_sha"],
    ).verify(settings["release"], now_ms=START - 10)
    with ReleaseFloor(root / "trust/floor.sqlite", initialize=True) as floor:
        floor.accept(release)
    with Journal(root / "guardian/account.sqlite", "fixture-account") as journal:
        journal.halt("MANUAL_HALT", START - 11)
    return authority, CopyMandate.parse(body["mandates"][0])


def gap_packet():
    body = deepcopy(json.loads(fixture_packet())["payload"])
    old = body["transitions"][-1]
    before = old["successor"]
    boundary = before["next_checkpoint_ms"]
    now = boundary + 300_000
    stream = digest([SCOPE["source_id"], SCOPE["account_sha"], SCOPE["generation"], digest(POLICY)])
    effect = dict(
        old["effect"],
        checkpoint_ms=boundary,
        kind="UNAVAILABLE",
        outcome_id=digest(["cwsa_outcome_v3", stream, boundary]),
        economic_sha=None,
        signal_id=None,
        positions=[],
        sample=None,
        published_ms=now,
        reasons=["SCHEDULE_GAP"],
    )
    event = dict(
        old,
        sequence=4,
        prior_sha=digest(old),
        predecessor=before,
        event="GAP",
        evidence_sha=digest(["gap", boundary]),
        guard="SCHEDULE_GAP",
        at_ms=now,
        successor=dict(before, sequence=4, phase="FENCED", next_checkpoint_ms=boundary + 300_000),
        effect=effect,
    )
    body.update(issued_ms=now, transitions=[event], originals={})
    return sign(body, KEY, "cwsa-checkpoint-v3"), now


def test_existing_worker_shadow_baseline_gap_halt_and_restart_preserve_authority(tmp_path):
    root = tmp_path / "rehearsal"
    authority, mandate = setup(root)
    floor_before = (root / "trust/floor.sqlite").read_bytes()
    workers = []
    try:
        workers.append(Worker("broker", root))
        guardian = Worker("guardian", root)
        workers.append(guardian)
        initial = guardian.call("status")
        activated = guardian.call(
            "mandate_activate",
            envelope=sign(mandate.data, authority, "copy-mandate-v1"),
            now_ms=START - 1,
        )
        assert activated["phase"] == "AWAITING_BASELINE"
        packet = json.loads(fixture_packet())
        now = packet["payload"]["issued_ms"]
        assert guardian.call("checkpoint_ingest", envelope=packet, now_ms=now)["events"] == 3
        assert (
            guardian.call("mandate_baseline", owner=mandate.owner, now_ms=now)["phase"] == "ACTIVE"
        )
        assert not guardian.call("status")["intents"]
        gap, gap_now = gap_packet()
        assert guardian.call("checkpoint_ingest", envelope=gap, now_ms=gap_now)["events"] == 1
        rows = guardian.call("checkpoint_status", now_ms=gap_now)
        assert rows["sources"][SCOPE["source_id"]]["phase"] == "FENCED"
        wrong = dict(mandate.data, account_id="another-account")
        assert guardian.call(
            "mandate_activate", envelope=sign(wrong, authority, "copy-mandate-v1"), now_ms=START - 1
        ) == {"error": "REQUEST_REJECTED"}
        guardian.close()
        guardian = Worker("guardian", root)
        workers.append(guardian)
        final = guardian.call("status")
        for key in (
            "finance",
            "positions",
            "pod_positions",
            "fills",
            "intents",
            "reservations",
            "halts",
        ):
            assert final[key] == initial[key]
        assert final["runtime"]["permit"] is None
        assert guardian.call("checkpoint_status")["sources"][SCOPE["source_id"]]["cursor"] == 4
        assert (root / "trust/floor.sqlite").read_bytes() == floor_before
        control = LocalClient(
            root / "broker/b.sock", "judge", (root / "judge/judge.key").read_bytes(), os.getuid()
        )
        assert control.call({"command": "audit"})["audit"] == []
    finally:
        for worker in reversed(workers):
            worker.close()


def test_checkpoint_worker_refuses_legacy_planner(tmp_path):
    root = tmp_path / "rehearsal"
    setup(root)
    with pytest.raises(RuntimeError, match="CHECKPOINTS_BYPASS_LEGACY_PLANNER"):
        Worker("planner", root)
