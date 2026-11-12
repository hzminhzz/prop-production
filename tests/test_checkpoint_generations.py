"""Successor policy never adopts or erases the previous financial owner."""

from copy import deepcopy
from decimal import Decimal as D

import pytest
from checkpoint_scenarios import quotes, scenario
from test_checkpoint_netting import decide, reserve
from test_checkpoint_receiver import KEY
from test_copy_mandates import START

from prop_production.copy_mandates import CopyMandate
from prop_production.evidence import digest, sign
from prop_production.guardian import Guardian
from prop_production.journal import Journal
from prop_production.release import ReleaseVerifier
from prop_production.v4_source_receiver import SourcePacketReceiver


def stop(s, owner, event="RETIRE"):
    row = s.journal.snapshot()["copy_mandates"][owner]
    command = sign(
        {
            "schema": "copy_mandate_control_v1",
            "owner": owner,
            "mandate_sha": digest(row["mandate"]),
            "predecessor_sha": digest(row["state"]),
            "event": event,
            "issued_ms": s.now,
            "expires_ms": s.now + 1000,
            "nonce": digest([owner, event, s.now]),
        },
        s.authority,
        "copy-mandate-control-v1",
    )
    return s.guardian.control_copy_mandate(command, now_ms=s.now)


def successor(s, *, retire=True, policy=None):
    if retire:
        stop(s, s.mandates[0].owner)
    body = deepcopy(s.release_body)
    body.update(generation=body["generation"] + 1, created_at_ms=s.now)
    new_policy = deepcopy(policy or s.policy)
    body["risk_policy_sha"] = digest({"policy": new_policy, "markets": s.markets})
    m = dict(
        body["mandates"][0],
        generation=2,
        k="0.01",
        effective_from_ms=START + 600000,
        risk_policy_sha=body["risk_policy_sha"],
    )
    body["mandates"][0] = m
    body["members"][0]["k"] = m["k"]
    body["definition_sha"] = digest(body["members"])
    body["source_sha"] = digest(
        {
            k: body[k]
            for k in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    release = ReleaseVerifier(
        s.authority.public_key(),
        "approver",
        "fixture-account",
        body["risk_policy_sha"],
        s.guardian.rules.sha,
    ).verify(sign(body, s.authority, "approver"), now_ms=s.now)
    s.floor.accept(release)
    s.receiver.rebind_checkpoint_release(
        release,
        authority_key=s.authority.public_key(),
        floor=s.floor,
        now_ms=s.now,
    )
    s.guardian = Guardian(
        s.journal,
        release,
        s.floor,
        KEY.public_key(),
        s.guardian.rules,
        new_policy,
        s.markets,
        s.broker,
        mandate_key=s.authority.public_key(),
    )
    s.release = release
    new = CopyMandate.parse(m)
    result = s.guardian.activate_copy_mandate(
        sign(m, s.authority, "copy-mandate-v1"),
        now_ms=s.now,
    )
    assert result.phase == "AWAITING_BASELINE"
    return new


def test_changed_activation_baseline_cannot_open_current_position(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),), effective_from=START + 300000) as s:
        owner = s.mandates[0].owner
        assert s.journal.snapshot()["copy_mandates"][owner]["state"]["phase"] == "AWAITING_BASELINE"
        s.guardian.baseline_copy_mandate(owner, s.receiver, now_ms=s.now)
        result = decide(s)
        assert result["status"] == "SOURCE_ONLY"
        assert result["source_dispositions"][owner] == "ACTIVATION_BASELINE"
        assert not s.journal.snapshot().get("checkpoint_opportunities")
        assert not s.journal.snapshot()["intents"]


def test_mandate_baseline_cannot_be_replaced_by_unproved_metadata(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        state = s.journal.snapshot()
        owner = s.mandates[0].owner
        state["copy_mandates"][owner]["baseline_proof"]["outcome"]["positions"][0]["quantity"] = (
            "999"
        )
        with pytest.raises(ValueError, match="MANDATE_BASELINE"):
            s.journal.commit("FORGED_BASELINE", s.now, state, expected_sequence=s.journal.sequence)


@pytest.mark.parametrize("fault", ["PARTIAL", "UNKNOWN"])
def test_replacement_preserves_old_effects_and_source_history_across_restart(
    tmp_path, fault, request
):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        if fault == "PARTIAL":
            s.broker.fill_on_submit = False
        else:
            s.broker.fault_mode = "ACCEPT_THEN_TIMEOUT"
        ids = reserve(s, decide(s))
        s.guardian.dispatch(ids[0], s.now + 2)
        if fault == "PARTIAL":
            s.broker.fill(ids[0], "0.4", "10030")
            s.guardian.reconcile(s.now + 3)
            partial = s.journal.snapshot()
            assert D(partial["positions"]["BTC"]) == D("0.03")
            assert partial["intents"][ids[0]]["status"] == "PARTIAL"
            assert partial["reservations"]
            assert D(partial["pod_fees_paid"][s.mandates[0].owner]) > 0
        s.now += 10
        before = s.journal.snapshot()
        preserved = {
            k: deepcopy(before[k])
            for k in (
                "finance",
                "positions",
                "pod_positions",
                "fills",
                "intents",
                "reservations",
            )
        }
        source_history = s.receiver.checkpoint_outcomes(now_ms=s.now)
        old_owner = s.mandates[0].owner
        new = successor(s)
        state = s.journal.snapshot()
        assert {k: state[k] for k in preserved} == preserved
        assert set(before["halts"]).issubset(state["halts"])
        assert state["copy_mandates"][old_owner]["state"]["phase"] == "RETIRED"
        assert new.owner not in state["pod_positions"]
        assert s.receiver.checkpoint_outcomes(now_ms=s.now) == source_history
        s.receiver.close()
        s.receiver = SourcePacketReceiver(
            s.root / "source.sqlite3",
            release=s.release,
            source_key=KEY.public_key(),
            wire_version=3,
        )
        request.addfinalizer(s.receiver.close)
        assert s.receiver.checkpoint_outcomes(now_ms=s.now) == source_history
        s.journal.close()
        s.journal = Journal(s.root / "guardian.sqlite3", "fixture-account")
        request.addfinalizer(s.journal.close)
        s.guardian = Guardian(
            s.journal,
            s.release,
            s.floor,
            KEY.public_key(),
            s.guardian.rules,
            s.policy,
            s.markets,
            s.broker,
            mandate_key=s.authority.public_key(),
        )
        state = s.journal.snapshot()
        assert {k: state[k] for k in preserved} == preserved
        # Ingest through the reopened source authority, without resetting its epoch.
        import json
        from pathlib import Path

        vectors = json.loads(
            (Path(__file__).parent / "fixtures/cwsa_checkpoint_vectors_v3.json").read_text()
        )
        for index in (2, 3):
            s.now = START + index * 300000 + 102
            packet = vectors["sources"][0]["packets"][index]
            s.receiver.ingest(json.dumps(packet).encode(), now_ms=s.now)
            outcome = packet["payload"]["transitions"][-1]["effect"]["outcome_id"]
            if index == 2:
                s.guardian.baseline_copy_mandate(new.owner, s.receiver, now_ms=s.now)
            result = s.guardian.review_checkpoints(
                s.receiver,
                {new.owner: outcome},
                quotes=quotes(s.now),
                now_ms=s.now,
            )
            assert result["status"] == ("SOURCE_ONLY" if index == 2 else "MISSED")
        assert {k: s.journal.snapshot()[k] for k in preserved} == preserved
        assert len([a for a in s.broker.audit() if a["kind"] == "SUBMIT"]) == 1
        s.journal.verify()


def test_release_change_keeps_unaffected_mandate_and_rejects_downgrade(tmp_path):
    with scenario(tmp_path) as s:
        old_release = s.release
        owner_b = s.mandates[1].owner
        before_b = deepcopy(s.journal.snapshot()["copy_mandates"][owner_b])
        successor(s)
        assert s.journal.snapshot()["copy_mandates"][owner_b] == before_b
        with pytest.raises(ValueError, match="ROLLBACK"):
            s.floor.accept(old_release)
        with pytest.raises(ValueError, match="ROLLBACK|FORWARD"):
            s.receiver.rebind_checkpoint_release(
                old_release,
                authority_key=s.authority.public_key(),
                floor=s.floor,
                now_ms=s.now,
            )


def recover_release(s):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_recovery_authenticated import request_for

    from prop_production.recovery import snapshot_receipt

    key = Ed25519PrivateKey.generate()
    s.guardian.reconcile(s.now)
    evidence_sha = digest(snapshot_receipt(s.broker.observe(s.now)))
    command = request_for(
        s.journal,
        key,
        evidence_sha,
        now=s.now,
        reason="RELEASE_CHANGED_REQUIRES_REVIEW",
    )
    s.journal.recover_authenticated(
        command,
        recovery_key=key.public_key(),
        gateway=s.broker,
        now_ms=s.now,
    )


def test_successor_cannot_offset_old_owner_and_flatten_requires_separate_signature(tmp_path):
    from test_checkpoint_protection import request
    from test_checkpoint_reversal import opened

    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        old_owner = s.mandates[0].owner
        old_positions = deepcopy(s.journal.snapshot()["pod_positions"])
        new = successor(s)
        s.now, old_requests = s.wave(2)
        s.guardian.baseline_copy_mandate(new.owner, s.receiver, now_ms=s.now)
        recover_release(s)
        assert not s.journal.snapshot()["halts"]
        s.now, old_requests = s.wave(3)
        s.guardian.reconcile(s.now)
        s.requests = {new.owner: old_requests[old_owner]}
        result = decide(s)
        assert result["status"] == "REVIEWABLE"
        assert result["execution_plan"]["opening_status"] == "MISSED"
        assert reserve(s, result) == []
        assert s.journal.snapshot()["pod_positions"] == old_positions
        assert len([a for a in s.broker.audit() if a["kind"] == "SUBMIT"]) == 1
        # Retirement and the new baseline did not flatten; this distinct command can.
        s.now += 10
        s.guardian.reconcile(s.now)
        identity = s.guardian.protective_reduce(request(s), quotes=quotes(s.now), now_ms=s.now)
        s.guardian.dispatch(identity, s.now + 1)
        s.guardian.reconcile(s.now + 2)
        state = s.journal.snapshot()
        assert not state["halts"]
        assert not s.broker.observe(s.now + 2).positions
        assert D(state["pod_positions"][old_owner]["BTC"]) == 0
        assert new.owner not in state["pod_positions"]
        assert state["copy_mandates"][old_owner]["state"]["phase"] == "RETIRED"
        s.journal.verify()


def test_relaxed_successor_policy_cannot_erase_original_owner_loss_limit(tmp_path):
    from test_checkpoint_reversal import opened

    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        owner = s.mandates[0].owner
        relaxed = dict(s.policy, pod_loss_budget="10000")
        successor(s, policy=relaxed)
        s.now += 10
        s.broker.mark("BTC", "7000", s.now)
        s.guardian.reconcile(s.now)
        state = s.journal.snapshot()
        assert D(state["pod_loss_used"][owner]) > D("200")
        assert "POD_DAILY_STOP" in state["halts"]
        assert state["copy_mandates"][owner]["risk_contract"]["policy"]["pod_loss_budget"] == "200"


def test_unaffected_owner_continues_when_retired_generation_is_in_same_review(tmp_path):
    with scenario(tmp_path) as s:
        old_a, b = s.mandates
        new = successor(s)
        s.now, s.requests = s.wave(2)
        s.guardian.baseline_copy_mandate(new.owner, s.receiver, now_ms=s.now)
        recover_release(s)
        s.requests[new.owner] = s.requests[old_a.owner]
        result = decide(s)
        assert result["status"] == "REVIEWABLE"
        assert result["source_dispositions"][old_a.owner] == "RETIRED_GENERATION"
        assert result["source_dispositions"][new.owner] == "ACTIVATION_BASELINE"
        decision = s.journal.snapshot()["checkpoint_decisions"][result["decision_id"]]
        assert {p["pod_id"] for p in decision["attempts"][-1]["pods"]} == {b.owner}


def test_mandate_metadata_transaction_cannot_clear_old_fees(tmp_path):
    from test_checkpoint_reversal import opened

    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        state = s.journal.snapshot()
        assert state["pod_fees_paid"]
        state["pod_fees_paid"] = {}
        with pytest.raises(ValueError, match="MANDATE_TRANSITION_MUTATED_FINANCIAL_OBLIGATIONS"):
            s.journal.commit(
                "COPY_MANDATE_CONTROL",
                s.now + 10,
                state,
                expected_sequence=s.journal.sequence,
            )


def test_release_halt_cannot_be_cleared_before_successor_baseline(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        successor(s)
        with pytest.raises(ValueError, match="SUCCESSION_UNPROVEN"):
            recover_release(s)
        assert "RELEASE_CHANGED_REQUIRES_REVIEW" in s.journal.snapshot()["halts"]


def test_new_generation_requires_explicit_stop_of_overlapping_old_generation(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        with pytest.raises(ValueError, match="OPENING_INTERVAL_OVERLAP"):
            successor(s, retire=False)
        assert len(s.journal.snapshot()["copy_mandates"]) == 1
