"""Persisted financial guards must independently reject forged account evidence."""

from copy import deepcopy

import pytest
from checkpoint_scenarios import quotes, scenario
from test_checkpoint_netting import decide
from test_checkpoint_protection import request
from test_checkpoint_reversal import opened

from prop_production.guardian_protection import derive


def corrupt(proof, fault):
    if fault == "positions":
        proof["positions"] = {"BTC": "999"}
    elif fault == "cash":
        proof["cash"] = "1"
    elif fault == "marks":
        proof["market_marks"] = [["BTC", "99999", proof["observed_at_ms"]]]
    elif fault == "snapshot_incomplete":
        proof["snapshot"]["history_complete"] = False
    else:
        proof["cutoff"] = "unreconciled-cutoff"


@pytest.mark.parametrize("fault", ["positions", "cash", "marks", "cutoff", "snapshot_incomplete"])
def test_review_oracle_revalidates_independent_account_proof(tmp_path, monkeypatch, fault):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        original = s.journal.commit

        def forged(kind, now, state, **kwargs):
            if kind == "CHECKPOINT_REVIEW":
                record = next(iter(state["checkpoint_decisions"].values()))
                corrupt(record["attempts"][-1]["account_proof"], fault)
            return original(kind, now, state, **kwargs)

        monkeypatch.setattr(s.journal, "commit", forged)
        with pytest.raises(ValueError, match="ACCOUNT_PROOF"):
            decide(s)
        assert not s.journal.snapshot()["intents"]
        assert not s.broker.audit()


@pytest.mark.parametrize("fault", ["positions", "cash", "marks", "cutoff", "snapshot_incomplete"])
def test_protection_oracle_revalidates_same_account_proof(tmp_path, fault):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.now += 10000
        s.guardian.reconcile(s.now)
        before = s.journal.snapshot()
        identity = s.guardian.protective_reduce(request(s), quotes=quotes(s.now), now_ms=s.now)
        state = s.journal.snapshot()
        protection = state["intents"][identity]["protection_id"]
        record = deepcopy(state["protective_reductions"][protection]["record"])
        corrupt(record["account_proof"], fault)
        with pytest.raises(ValueError, match="ACCOUNT_PROOF"):
            derive(before, record)
        assert len(s.broker.audit()) == 1


def test_new_provider_cutoff_requires_reconciliation_before_any_opening(tmp_path):
    from dataclasses import replace

    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        s.guardian.reconcile(s.now)
        observed = s.broker.observe
        s.broker.observe = lambda now: replace(observed(now), cutoff="later-independent-cutoff")
        result = s.guardian.review_checkpoints(
            s.receiver, s.requests, quotes=quotes(s.now), now_ms=s.now
        )
        assert result["status"] == "PRE_DISPATCH_BLOCKED"
        assert any("ACCOUNT_PROOF" in reason for reason in result["reasons"])
        assert not s.journal.snapshot()["intents"]
        assert not s.broker.audit()
