"""A relaxed successor cannot weaken fresh evidence for retained old exposure."""

from copy import deepcopy

import pytest
from checkpoint_scenarios import quotes, scenario
from test_checkpoint_generations import recover_release, successor
from test_checkpoint_protection import request
from test_checkpoint_reversal import opened


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
            s.guardian.protective_reduce(request(s), quotes=quotes(s.now - 3000), now_ms=s.now)
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
        identity = s.guardian.protective_reduce(request(s), quotes=quotes(s.now), now_ms=s.now)
        intent = s.journal.snapshot()["intents"][identity]
        assert intent["expires_at_ms"] <= cutoff + 2000
        assert intent["account_max_age_ms"] == 2000
        assert len(s.broker.audit()) == 1
