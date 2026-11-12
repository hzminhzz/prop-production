"""A relaxed successor cannot weaken fresh evidence for retained old exposure."""

from copy import deepcopy

import pytest
from checkpoint_scenarios import quotes, scenario
from test_checkpoint_generations import recover_release, successor
from test_checkpoint_netting import decide, reserve
from test_checkpoint_protection import request as protective_request
from test_checkpoint_receiver import KEY
from test_checkpoint_reversal import opened

from prop_production.guardian import Guardian
from prop_production.journal import Journal


def test_retained_owner_keeps_stricter_quote_age_on_signed_reduction(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        relaxed = deepcopy(s.policy)
        relaxed["checkpoint_execution"].update(quote_max_age_ms=4500, account_max_age_ms=4500)
        successor(s, policy=relaxed)
        s.now += 10
        s.guardian.reconcile(s.now)
        before = s.journal.head
        with pytest.raises(ValueError, match="STALE|ACCOUNT_PROOF"):
            s.guardian.protective_reduce(
                protective_request(s), quotes=quotes(s.now - 3000), now_ms=s.now
            )
        assert s.journal.head == before
        assert len(s.broker.audit()) == 1


def test_retained_owner_deadline_uses_original_account_freshness(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        relaxed = deepcopy(s.policy)
        relaxed["checkpoint_execution"].update(quote_max_age_ms=4500, account_max_age_ms=4500)
        new = successor(s, policy=relaxed)
        s.now, _ = s.wave(2)
        s.guardian.baseline_copy_mandate(new.owner, s.receiver, now_ms=s.now)
        recover_release(s)
        s.now += 1500
        # Original reconciliation is still fresh under the old 2000 ms contract.
        cutoff = s.journal.snapshot()["runtime"]["last_snapshot_ms"]
        assert 0 < s.now - cutoff < 2000
        identity = s.guardian.protective_reduce(
            protective_request(s), quotes=quotes(s.now), now_ms=s.now
        )
        intent = s.journal.snapshot()["intents"][identity]
        assert intent["expires_at_ms"] <= cutoff + 2000
        assert intent["account_max_age_ms"] == 2000
        assert len(s.broker.audit()) == 1


def test_successor_cannot_relax_retained_quote_limit_after_halt_and_restart(tmp_path, request):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        old_owner = s.mandates[0].owner
        relaxed = deepcopy(s.policy)
        relaxed["checkpoint_execution"].update(quote_max_age_ms=4500, account_max_age_ms=4500)
        successor(s, policy=relaxed)
        s.now += 10
        s.journal.halt("operator incident", s.now)
        before_halts = deepcopy(s.journal.snapshot()["halts"])
        s.journal.close()
        s.journal = Journal(s.root / "guardian.sqlite3", "fixture-account")
        request.addfinalizer(s.journal.close)
        s.guardian = Guardian(
            s.journal,
            s.release,
            s.floor,
            KEY.public_key(),
            s.guardian.rules,
            relaxed,
            s.markets,
            s.broker,
            mandate_key=s.authority.public_key(),
        )
        s.guardian.reconcile(s.now)
        assert s.journal.snapshot()["halts"] == before_halts
        assert s.journal.snapshot()["pod_positions"][old_owner]["BTC"] == "0.075"
        before_head = s.journal.head
        with pytest.raises(ValueError, match="STALE|ACCOUNT_PROOF"):
            s.guardian.protective_reduce(
                protective_request(s), quotes=quotes(s.now - 3000), now_ms=s.now
            )
        assert s.journal.head == before_head
        assert s.journal.snapshot()["halts"] == before_halts
        assert len(s.broker.audit()) == 1
        s.journal.verify()


def test_partially_filled_old_owner_blocks_protection_under_relaxed_successor(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        s.broker.fill_on_submit = False
        ids = reserve(s, decide(s))
        s.guardian.dispatch(ids[0], s.now + 2)
        s.broker.fill(ids[0], "0.4", "10030")
        s.guardian.reconcile(s.now + 3)
        old_owner = s.mandates[0].owner
        assert s.journal.snapshot()["intents"][ids[0]]["status"] == "PARTIAL"
        relaxed = deepcopy(s.policy)
        relaxed["checkpoint_execution"].update(quote_max_age_ms=4500, account_max_age_ms=4500)
        successor(s, policy=relaxed)
        s.guardian.reconcile(s.now)
        before = s.journal.snapshot()
        with pytest.raises(ValueError, match="PROTECTIVE_UNRESOLVED_OBLIGATIONS"):
            s.guardian.protective_reduce(protective_request(s), quotes=quotes(s.now), now_ms=s.now)
        after = s.journal.snapshot()
        assert after["intents"] == before["intents"]
        assert after["reservations"] == before["reservations"]
        assert after["pod_positions"][old_owner] == before["pod_positions"][old_owner]
        assert len(s.broker.audit()) == 1
        s.journal.verify()


def test_reversal_under_successor_cannot_erase_old_owner_exposure(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        old_owner = s.mandates[0].owner
        before = deepcopy(s.journal.snapshot()["pod_positions"])
        relaxed = deepcopy(s.policy)
        relaxed["checkpoint_execution"].update(quote_max_age_ms=4500, account_max_age_ms=4500)
        new = successor(s, policy=relaxed)
        s.now, _ = s.wave(2)
        s.guardian.baseline_copy_mandate(new.owner, s.receiver, now_ms=s.now)
        recover_release(s)
        s.now, old_requests = s.wave(3)
        s.guardian.reconcile(s.now)
        s.requests = {new.owner: old_requests[old_owner]}
        result = decide(s)
        assert result["execution_plan"]["opening_status"] == "MISSED"
        assert reserve(s, result) == []
        assert s.journal.snapshot()["pod_positions"] == before
        assert old_owner in s.journal.snapshot()["pod_positions"]
        assert len(s.broker.audit()) == 1
        s.journal.verify()
