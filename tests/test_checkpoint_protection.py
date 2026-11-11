"""Explicit signed reductions are independent of the source grid and preserve HALT."""

from decimal import Decimal as D

import pytest
from checkpoint_scenarios import quotes, scenario
from test_checkpoint_reversal import opened

from prop_production.evidence import digest, sign


def request(s, *, quantity="-0.075", nonce="stop"):
    return sign(
        {
            "schema": "guardian_protective_reduction_v1",
            "account_binding": "fixture-account",
            "release_sha": s.release.release_sha,
            "predecessor_sha": s.journal.head,
            "broker_epoch": s.journal.snapshot()["runtime"]["broker_epoch"],
            "market": "BTC",
            "quantity": quantity,
            "issued_ms": s.now,
            "expires_ms": s.now + 1000,
            "reason": "EXPLICIT_FLATTEN",
            "nonce": digest(nonce),
        },
        s.authority,
        "guardian-protection-v1",
    )


def test_explicit_protected_reduction_between_checkpoints_preserves_financial_halt(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.now += 17000
        s.journal.halt("incident", s.now)
        s.guardian.reconcile(s.now)
        halt = s.journal.snapshot()["halts"]
        command = request(s)
        identity = s.guardian.protective_reduce(command, quotes=quotes(s.now), now_ms=s.now)
        state = s.journal.snapshot()
        assert state["intents"][identity]["reduce_only"] is True
        assert state["reservations"]
        assert (
            s.guardian.protective_reduce(command, quotes=quotes(s.now), now_ms=s.now + 1)
            == identity
        )
        s.guardian.dispatch(identity, s.now + 2)
        s.guardian.reconcile(s.now + 3)
        state = s.journal.snapshot()
        assert state["halts"] == halt
        assert not s.broker.observe(s.now + 3).positions
        assert D(state["positions"]["BTC"]) == 0
        assert not state["reservations"]
        assert len([a for a in s.broker.audit() if a["kind"] == "SUBMIT"]) == 2
        s.journal.verify()


def test_reduce_only_flag_cannot_bypass_halt_without_signed_protection(tmp_path):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        identity = opened(s)
        s.journal.halt("incident", s.now + 4)
        state = s.journal.snapshot()
        state["runtime"]["permit"] = {
            "decision_id": state["intents"][identity]["decision_id"],
            "boot_epoch": state["runtime"]["boot_epoch"],
            "broker_epoch": state["runtime"]["broker_epoch"],
            "expires_at_ms": s.now + 1000,
            "reduce_only": True,
        }
        with pytest.raises(ValueError, match="HALT_OR_PERMIT_INVALID"):
            s.journal.commit(
                "FORGED_PERMISSION", s.now + 5, state, expected_sequence=s.journal.sequence
            )


@pytest.mark.parametrize("quantity", ["0.01", "-0.08"])
def test_signed_operator_cannot_use_protection_to_add_or_reverse_exposure(tmp_path, quantity):
    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        opened(s)
        s.now += 10000
        s.guardian.reconcile(s.now)
        with pytest.raises(ValueError, match="PROTECTIVE_REDUCTION_BOUND"):
            s.guardian.protective_reduce(
                request(s, quantity=quantity), quotes=quotes(s.now), now_ms=s.now
            )
        assert len(s.journal.snapshot()["intents"]) == 1


def test_unknown_or_pending_order_blocks_protection_without_assuming_no_fill(tmp_path):
    from test_checkpoint_netting import decide, reserve

    with scenario(tmp_path, copies=((0, "0.025"),)) as s:
        s.broker.fault_mode = "ACCEPT_THEN_TIMEOUT"
        ids = reserve(s, decide(s))
        s.guardian.dispatch(ids[0], s.now + 2)
        with pytest.raises(ValueError, match="PROTECTIVE_UNRESOLVED_OBLIGATIONS"):
            s.guardian.protective_reduce(request(s), quotes=quotes(s.now), now_ms=s.now)
        assert s.journal.snapshot()["reservations"]
        assert len(s.broker.audit()) == 1
