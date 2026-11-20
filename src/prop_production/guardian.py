"""The single account financial writer and sole synthetic dispatch authority."""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from hashlib import sha256
from typing import Any

from copytrade_kernel import DecisionMs
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .checkpoint_accounting import add, apply_fill, apply_funding
from .checkpoint_admission import prepare_reserve, prepare_review
from .checkpoint_receiver import CheckpointSourceReceiver
from .checkpoint_risk import loss_limit
from .contracts import (
    ALLOWED_BROKER_ORDER_STATUSES,
    CHECKPOINT_POLICY_V3,
    PORTFOLIO_RELEASE_V2,
    SYNTHETIC_ENDPOINT,
    TERMINAL_INTENT_STATUSES,
    is_synthetic_account,
)
from .copy_mandates import (
    MandateState,
    prepare_activate_mandate,
    prepare_baseline_mandate,
    prepare_control_mandate,
)
from .evidence import decimal, digest, load_json
from .finance import Rulebook, account_step
from .gateway import BrokerGateway, BrokerOrder, BrokerSnapshot, Execution, FundingEvent
from .guardian_protection import prepare_protective_reduction
from .journal import Journal, State, StorageUnavailable
from .release import ReleaseFloor, VerifiedRelease

TERMINAL = TERMINAL_INTENT_STATUSES
ZERO = Decimal("0")


def _halt(state: State, reason: str) -> None:
    if reason not in state["halts"]:
        state["halts"].append(reason)
    state["runtime"].update(
        state="UNRESOLVED", certainty="UNKNOWN", permit=None, flat_confirmed=False
    )


class Guardian:
    def __init__(
        self,
        journal: Journal,
        release: VerifiedRelease,
        floor: ReleaseFloor,
        source_key: Ed25519PublicKey,
        rules: Rulebook,
        policy: dict[str, Any],
        markets: dict[str, Any],
        gateway: BrokerGateway,
        recovery_key: Ed25519PublicKey | None = None,
        expected_recovery_key_sha: str | None = None,
        revoked_recovery_key_shas: frozenset[str] = frozenset(),
        revoked_recovery_nonces: frozenset[str] = frozenset(),
        mandate_key: Ed25519PublicKey | None = None,
    ):
        self.journal, self.release, self.floor = journal, release, floor
        self.source_key, self.rules = source_key, rules
        if (
            mandate_key is not None
            and mandate_key.public_bytes_raw() == source_key.public_bytes_raw()
        ):
            raise ValueError("MANDATE_AUTHORITY_MUST_BE_DISTINCT_FROM_SOURCE")
        self.mandate_key = mandate_key
        self.policy, self.markets, self.gateway = policy, markets, gateway
        if (
            recovery_key is not None
            and recovery_key.public_bytes_raw() == source_key.public_bytes_raw()
        ):
            raise ValueError("GUARDIAN_RECOVERY_SIGNER_MUST_BE_DISTINCT")
        self.recovery_key = recovery_key
        self.expected_recovery_key_sha = expected_recovery_key_sha
        if (
            not is_synthetic_account(journal.snapshot()["account_binding"])
            and recovery_key is not None
        ):
            current_sha = sha256(recovery_key.public_bytes_raw()).hexdigest()
            if (
                not isinstance(expected_recovery_key_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_recovery_key_sha) is None
                or expected_recovery_key_sha != current_sha
            ):
                raise ValueError("GUARDIAN_RECOVERY_KEY_NOT_INDEPENDENTLY_PINNED")
        self.revoked_recovery_key_shas = revoked_recovery_key_shas
        self.revoked_recovery_nonces = revoked_recovery_nonces
        self.fault: Callable[[str], None] | None = None
        if journal.snapshot()["account_binding"] != release.payload["account_binding"]:
            raise ValueError("ACCOUNT_BINDING")
        if (
            digest({"policy": policy, "markets": markets}) != release.payload["risk_policy_sha"]
            or rules.sha != release.payload["rulebook_sha"]
        ):
            raise ValueError("POLICY_BINDING")
        floor.require_active(release)
        source_public = source_key.public_bytes_raw().hex()
        if release.payload["schema"] != PORTFOLIO_RELEASE_V2:
            raise ValueError("UNSUPPORTED_RELEASE_SCHEMA")
        if (
            mandate_key is None
            or release.signed_envelope is None
            or sha256(mandate_key.public_bytes_raw()).hexdigest()
            != release.payload["mandate_authority_sha"]
        ):
            raise ValueError("CHECKPOINT_MANDATE_AUTHORITY_NOT_PINNED")
        pinned = journal.snapshot().get("checkpoint_source_public")
        if pinned is not None and pinned != source_public:
            raise ValueError("CHECKPOINT_SOURCE_AUTHORITY_CHANGED")
        active = journal.snapshot().get("active_release_sha")
        if active != release.release_sha:

            def bind(s: State) -> None:
                if active is not None:
                    _halt(s, "RELEASE_CHANGED_REQUIRES_REVIEW")
                s["active_release_sha"] = release.release_sha
                s["checkpoint_source_public"] = source_public
                assert mandate_key is not None and release.signed_envelope is not None
                s["checkpoint_release"] = {
                    "envelope": load_json(release.signed_envelope),
                    "authority_public": mandate_key.public_bytes_raw().hex(),
                }
                s["runtime"]["permit"] = None

            journal.update("BIND_RELEASE_DISARMED", 0, bind)

    def _verify_checkpoint_source(self, receiver: CheckpointSourceReceiver) -> None:
        p = self.release.payload
        if (
            receiver.wire_version != 3
            or receiver.source_key.public_bytes_raw() != self.source_key.public_bytes_raw()
            or receiver.release.release_sha != self.release.release_sha
            or receiver.pins != {"policy": p["checkpoint_policy"], "scopes": p["checkpoint_scopes"]}
        ):
            raise ValueError("CHECKPOINT_SOURCE_AUTHORITY")

    def activate_copy_mandate(self, envelope: dict[str, Any], *, now_ms: int) -> MandateState:
        state_obj, owner, record = prepare_activate_mandate(
            self.journal.snapshot(),
            mandate_key=self.mandate_key,
            source_key=self.source_key,
            release_payload=self.release.payload,
            signed_release=self.release.signed_envelope,
            release_sha=self.release.release_sha,
            policy=self.policy,
            markets=self.markets,
            envelope=envelope,
            scope_reasons=self._scope(now_ms),
            now_ms=now_ms,
        )
        if owner is not None and record is not None:
            self.journal.update(
                "COPY_MANDATE_ACTIVATE",
                now_ms,
                lambda s: s.setdefault("copy_mandates", {}).update({owner: record}),
            )
        return state_obj

    def control_copy_mandate(self, envelope: dict[str, Any], *, now_ms: int) -> MandateState:
        state_obj, update = prepare_control_mandate(
            self.journal.snapshot(),
            mandate_key=self.mandate_key,
            release_payload=self.release.payload,
            has_signed_release=self.release.signed_envelope is not None,
            envelope=envelope,
            now_ms=now_ms,
        )
        if update is not None:

            def commit(s: State) -> None:
                row = s["copy_mandates"][update["owner"]]
                row["state"] = update["state"]
                row["history"].append(update["transition"])
                row["control_envelopes"][update["command_sha"]] = envelope
                s.setdefault("mandate_control_nonces", {})[update["nonce"]] = {
                    "command_sha": update["command_sha"],
                    "result": update["state"],
                }
                s["runtime"]["permit"] = None

            self.journal.update("COPY_MANDATE_CONTROL", now_ms, commit)
        return state_obj

    def baseline_copy_mandate(
        self, owner: str, receiver: CheckpointSourceReceiver, *, now_ms: int
    ) -> MandateState:
        self._verify_checkpoint_source(receiver)
        entry = self.journal.snapshot()["copy_mandates"][owner]
        state_obj, update = prepare_baseline_mandate(
            entry,
            outcomes=receiver.checkpoint_outcomes(now_ms=now_ms),
            packet_for=receiver.packet_for,
            now_ms=now_ms,
        )
        if update is not None:

            def commit(s: State) -> None:
                row = s["copy_mandates"][owner]
                row["state"] = update["state"]
                row["history"].append(update["transition"])
                row["baseline_proof"] = update["baseline_proof"]
                row["baseline_envelope"] = update["baseline_envelope"]

            self.journal.update("COPY_MANDATE_BASELINE", now_ms, commit)
        return state_obj

    def review_checkpoints(
        self,
        receiver: CheckpointSourceReceiver,
        requests: dict[str, str],
        *,
        quotes: dict[str, Any],
        now_ms: int,
        quote_envelope: dict[str, Any] | None = None,
        quote_key: Ed25519PublicKey | None = None,
    ) -> dict[str, Any]:
        return self.process_checkpoint(
            receiver,
            requests,
            quotes=quotes,
            now_ms=DecisionMs(now_ms),
            quote_envelope=quote_envelope,
            quote_key=quote_key,
        )

    def process_checkpoint(
        self,
        receiver: CheckpointSourceReceiver,
        requests: dict[str, str],
        *,
        quotes: dict[str, Any],
        now_ms: DecisionMs,
        quote_envelope: dict[str, Any] | None = None,
        quote_key: Ed25519PublicKey | None = None,
    ) -> dict[str, Any]:
        """Review a signed prospective checkpoint and durably record its decision."""
        self._verify_checkpoint_source(receiver)
        clock = now_ms.value
        result, next_state = prepare_review(
            self.journal.snapshot(),
            journal_sequence=self.journal.sequence,
            release=self.release,
            source_key=self.source_key,
            mandate_key=self.mandate_key,
            policy=self.policy,
            markets=self.markets,
            gateway=self.gateway,
            scope_reasons=self._scope(clock),
            source_outcomes=receiver.checkpoint_outcomes(now_ms=clock),
            packet_for=receiver.packet_for,
            requests=requests,
            quotes=quotes,
            now_ms=clock,
            quote_envelope=quote_envelope,
            quote_key=quote_key,
        )
        if next_state is not None:
            self.journal.commit(
                "CHECKPOINT_REVIEW", clock, next_state, expected_sequence=self.journal.sequence
            )
        return result

    def reserve_checkpoint(
        self, decision_id: str, *, receiver: CheckpointSourceReceiver, now_ms: int
    ) -> list[str]:
        self._verify_checkpoint_source(receiver)
        ids, commit_kind, next_state = prepare_reserve(
            self.journal.snapshot(),
            journal_sequence=self.journal.sequence,
            release=self.release,
            source_key=self.source_key,
            mandate_key=self.mandate_key,
            rules_status=self.rules.status,
            policy=self.policy,
            markets=self.markets,
            gateway=self.gateway,
            scope_reasons=self._scope(now_ms),
            source_outcomes=receiver.checkpoint_outcomes(now_ms=now_ms),
            packet_for=receiver.packet_for,
            decision_id=decision_id,
            now_ms=now_ms,
        )
        if commit_kind == "CHECKPOINT_REVIEW":
            if next_state is not None:
                self.journal.commit(
                    "CHECKPOINT_REVIEW", now_ms, next_state, expected_sequence=self.journal.sequence
                )
            raise ValueError("CHECKPOINT_RESERVATION_NOT_PERMITTED")
        if commit_kind == "CHECKPOINT_RESERVE":
            assert next_state is not None
            self._boundary("before_checkpoint_reservation")
            self.journal.commit(
                "CHECKPOINT_RESERVE", now_ms, next_state, expected_sequence=self.journal.sequence
            )
            self._boundary("after_checkpoint_reservation")
        return ids

    def protective_reduce(
        self, envelope: dict[str, Any], *, quotes: dict[str, Any], now_ms: int
    ) -> str:
        intent_id, next_state = prepare_protective_reduction(
            self.journal.snapshot(),
            head_sha=self.journal.head,
            release_payload=self.release.payload,
            release_authority=self.release.authority,
            signed_release=self.release.signed_envelope,
            mandate_key=self.mandate_key,
            rules_status=self.rules.status,
            policy=self.policy,
            markets=self.markets,
            gateway=self.gateway,
            scope_reasons=self._scope(now_ms),
            envelope=envelope,
            quotes=quotes,
            now_ms=now_ms,
        )
        if next_state is not None:
            self._boundary("before_protective_reservation")
            self.journal.commit(
                "PROTECTIVE_RESERVE", now_ms, next_state, expected_sequence=self.journal.sequence
            )
            self._boundary("after_protective_reservation")
        return intent_id

    def recover_authenticated(self, signed_request: dict[str, Any], now_ms: int) -> int:
        """Trusted protected signer + independently fetched coherent broker state."""
        if self.recovery_key is None:
            raise PermissionError("GUARDIAN_RECOVERY_AUTHORITY_NOT_PINNED")
        return self.journal.recover_authenticated(
            signed_request,
            recovery_key=self.recovery_key,
            gateway=self.gateway,
            now_ms=now_ms,
            expected_recovery_key_sha=self.expected_recovery_key_sha,
            revoked_key_shas=set(self.revoked_recovery_key_shas),
            revoked_nonces=set(self.revoked_recovery_nonces),
            forbidden_key_shas={sha256(self.source_key.public_bytes_raw()).hexdigest()},
        )

    def _boundary(self, name: str) -> None:
        if self.fault:
            self.fault(name)

    def _scope(self, now_ms: int) -> list[str]:
        reasons = []
        self.floor.require_active(self.release)
        p = self.release.payload
        if not p["created_at_ms"] <= now_ms < p["expires_at_ms"]:
            reasons.append("RELEASE_EXPIRED")
        if not self.journal.storage_healthy:
            reasons.append("HALT_STORAGE")
        return reasons

    def dispatch(self, intent_id: str, now_ms: int) -> None:
        state = self.journal.snapshot()
        intent = state["intents"][intent_id]
        permit = state["runtime"]["permit"]
        if (
            self._scope(now_ms)
            or intent["release_sha"] != self.release.release_sha
            or (
                state["halts"]
                and not (
                    intent.get("protection_id")
                    and intent["reduce_only"] is True
                    and isinstance(permit, dict)
                    and permit.get("reduce_only") is True
                )
            )
            or intent["status"] != "QUEUED"
            or not permit
            or self.gateway.synthetic is not True
            or getattr(self.gateway, "endpoint", None) != SYNTHETIC_ENDPOINT
            or intent["endpoint"] != SYNTHETIC_ENDPOINT
            or intent["boot_epoch"] != state["runtime"]["boot_epoch"]
            or intent["broker_epoch"] != state["runtime"]["broker_epoch"]
            or permit["decision_id"] != intent["decision_id"]
            or (
                now_ms >= intent["expires_at_ms"]
                if intent.get("checkpoint_policy") == CHECKPOINT_POLICY_V3
                else now_ms > intent["expires_at_ms"]
            )
            or state["runtime"]["certainty"] != "COHERENT"
            or not 0
            <= now_ms - state["runtime"]["last_snapshot_ms"]
            <= intent.get("account_max_age_ms", 5000)
        ):
            raise ValueError("DISPATCH_NOT_PERMITTED")
        self.journal.update(
            "DISPATCHING",
            now_ms,
            lambda s: s["intents"][intent_id].update(status="DISPATCHING", sent_at_ms=now_ms),
        )
        wire = self.journal.snapshot()["intents"][intent_id]
        wire.update(commit_sequence=self.journal.sequence, commit_sha=self.journal.head)
        if self.journal.in_transaction:
            raise StorageUnavailable("NETWORK_INSIDE_TRANSACTION")
        self._boundary("before_send")
        try:
            outcome = self.gateway.submit(wire)
            self._boundary("after_send")
            if outcome.status not in {
                "ACCEPTED",
                "FILLED",
                "PARTIAL",
                "REJECTED",
                "CANCELLED",
                "EXPIRED",
            }:
                raise TimeoutError("OUTCOME_NOT_DEFINITIVE")
        except (TimeoutError, ConnectionError, OSError):

            def unknown(s: State) -> None:
                s["intents"][intent_id]["status"] = "UNKNOWN"
                _halt(s, "UNKNOWN_OUTCOME")

            self.journal.update("SEND_UNKNOWN", now_ms, unknown)
            return
        # Even a FILLED acknowledgement requires authoritative fills before reservation release.
        self.journal.update(
            "ACK",
            now_ms,
            lambda s: s["intents"][intent_id].update(
                status="ACKNOWLEDGED" if outcome.status != "REJECTED" else "REJECT_PENDING_PROOF",
                broker_id=outcome.broker_id,
            ),
        )
        self._boundary("after_ack")

    def cancel(self, intent_id: str, now_ms: int) -> None:
        state = self.journal.snapshot()
        if (
            self.gateway.synthetic is not True
            or getattr(self.gateway, "endpoint", None) != SYNTHETIC_ENDPOINT
            or intent_id not in state["intents"]
        ):
            raise PermissionError("CANCEL_NOT_PERMITTED")
        if state["intents"][intent_id]["status"] in TERMINAL:
            return
        self.journal.update(
            "CANCEL_REQUESTED",
            now_ms,
            lambda s: s["intents"][intent_id].update(status="CANCEL_PENDING"),
        )
        self._boundary("before_cancel")
        try:
            self.gateway.cancel(intent_id)
            self._boundary("after_cancel")
        except (TimeoutError, ConnectionError, OSError):
            self.journal.halt("CANCEL_UNKNOWN", now_ms)
        # No release or replacement based on a cancellation acknowledgement alone.

    def reconcile(self, now_ms: int) -> dict[str, Any]:
        try:
            snapshot = self.gateway.observe(now_ms)
        except (TimeoutError, ConnectionError, OSError):
            self.journal.update(
                "BROKER_UNREACHABLE", now_ms, lambda s: _halt(s, "BROKER_UNREACHABLE")
            )
            return {"state": "UNRESOLVED", "certainty": "UNKNOWN", "flat_confirmed": False}
        return self.apply_snapshot(snapshot, now_ms)

    def apply_snapshot(self, snapshot: BrokerSnapshot, now_ms: int) -> dict[str, Any]:
        state = self.journal.snapshot()
        state["runtime"].update(permit=None, broker_epoch=state["runtime"]["broker_epoch"] + 1)
        if snapshot.account_binding != state["account_binding"]:
            _halt(state, "BROKER_ACCOUNT_MISMATCH")
        elif not snapshot.ready(now_ms, 5000):
            _halt(state, "RECONCILIATION_UNCERTAIN")
        elif snapshot.observed_at_ms < state["runtime"].get("last_snapshot_ms", 0):
            _halt(state, "BROKER_CLOCK_REGRESSION")
        else:
            self._reconcile_economics(state, snapshot, now_ms)
        state["runtime"]["last_progress_ms"] = now_ms
        self.journal.commit("RECONCILE", now_ms, state, expected_sequence=self.journal.sequence)
        return {
            "state": state["runtime"]["state"],
            "certainty": state["runtime"]["certainty"],
            "flat_confirmed": state["runtime"].get("flat_confirmed", False),
        }

    def _reconcile_economics(self, state: State, snapshot: BrokerSnapshot, now_ms: int) -> None:
        errors = []
        used = state.setdefault("pod_loss_used", {})
        previous_marks = state.setdefault("last_marks", {})
        marks = {m: price for m, price, _ in snapshot.market_marks}
        for position in snapshot.positions:
            if position.market in marks and marks[position.market] != position.mark_price:
                errors.append("BROKER_MARK_CONFLICT")
            marks[position.market] = position.mark_price
        new_fees = ZERO
        new_realized = ZERO
        new_funding = ZERO
        # Charge every adverse move; gains never refill a pod budget during the day.
        for pod, book in state["pod_positions"].items():
            adverse = sum(
                (
                    max(ZERO, -decimal(q) * (marks[m] - decimal(previous_marks[m])))
                    for m, q in book.items()
                    if m in marks and m in previous_marks
                ),
                ZERO,
            )
            if any(decimal(q) and m not in marks for m, q in book.items()):
                errors.append("POD_MARK_UNKNOWN")
            # Broker cash belongs to the account; charging the full debit to
            # every pod would double-count common fees and funding.
            used[pod] = str(decimal(used.get(pod, "0")) + adverse)
        timeline: list[tuple[int, int, str, Execution | FundingEvent]] = [
            (f.known_at_ms, 0, f.execution_id, f) for f in snapshot.fills
        ]
        if snapshot.funding_events:
            timeline.extend((e.known_at_ms, 1, e.funding_id, e) for e in snapshot.funding_events)
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))
        for _ts, _rank, _item_id, item in timeline:
            if isinstance(item, Execution):
                fill = item
                body = {
                    "execution_id": fill.execution_id,
                    "intent_id": fill.intent_id,
                    "market": fill.market,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                    "known_at_ms": fill.known_at_ms,
                }
                prior = state["fills"].get(fill.execution_id)
                if prior:
                    if prior != body:
                        errors.append("FILL_ID_CONFLICT")
                    continue
                intent = state["intents"].get(fill.intent_id)
                if intent is None:
                    errors.append("ORPHAN_FILL")
                    continue
                previous_filled = decimal(intent["filled"])
                total = previous_filled + fill.quantity
                if (
                    fill.market != intent["market"]
                    or fill.quantity * decimal(intent["quantity"]) <= 0
                    or abs(total) > abs(decimal(intent["quantity"]))
                    or fill.price <= 0
                    or fill.fee < 0
                    or fill.known_at_ms > snapshot.observed_at_ms
                ):
                    errors.append("FILL_INCONSISTENT")
                    continue
                changes = {
                    pod: decimal(change) for pod, change in intent.get("pod_changes", {}).items()
                }
                gross_changes = sum((abs(change) for change in changes.values()), ZERO)
                if (
                    not changes
                    or not gross_changes
                    or sum(changes.values(), ZERO) != decimal(intent["quantity"])
                ):
                    errors.append("POD_FILL_ALLOCATION_INVALID")
                    continue
                before_quantity = decimal(state["positions"].get(fill.market, "0"))
                if (
                    before_quantity * fill.quantity < 0
                    and intent.get("checkpoint_policy") != CHECKPOINT_POLICY_V3
                ):
                    # Realized PnL needs independently confirmed pod entry basis.
                    errors.append("POD_REALIZED_ATTRIBUTION_UNVERIFIED")
                new_fees += fill.fee
                intent["filled"] = str(total)
                state["fills"][fill.execution_id] = body
                state["positions"][fill.market] = str(
                    decimal(state["positions"].get(fill.market, "0")) + fill.quantity
                )
                if intent.get("checkpoint_policy") == CHECKPOINT_POLICY_V3:
                    effects = apply_fill(
                        state,
                        body,
                        previous_filled=previous_filled,
                        before_quantity=before_quantity,
                    )
                    new_realized = add(
                        new_realized, *(decimal(e["realized"]) for e in effects.values())
                    )
                    for pod, effect in effects.items():
                        allocated = decimal(effect["quantity"])
                        execution_loss = max(
                            ZERO, -allocated * (marks.get(fill.market, fill.price) - fill.price)
                        )
                        used[pod] = str(
                            add(decimal(used.get(pod, "0")), decimal(effect["fee"]), execution_loss)
                        )
                    continue
                for pod, change in changes.items():
                    book = state["pod_positions"].setdefault(pod, {})
                    allocated = change * fill.quantity / decimal(intent["quantity"])
                    book[fill.market] = str(decimal(book.get(fill.market, "0")) + allocated)
                    # Gross allocation shares conserve the real broker fee even when
                    # two pods oppose one another and the net order is much smaller.
                    fee = fill.fee * abs(change) / gross_changes
                    pod_fees = state.setdefault("pod_fees_paid", {})
                    pod_fees[pod] = str(decimal(pod_fees.get(pod, "0")) + fee)
                    execution_loss = max(
                        ZERO, -allocated * (marks.get(fill.market, fill.price) - fill.price)
                    )
                    used[pod] = str(decimal(used.get(pod, "0")) + fee + execution_loss)
            else:
                ev = item
                body = {
                    "funding_id": ev.funding_id,
                    "market": ev.market,
                    "amount": str(ev.amount),
                    "position_quantity": str(ev.position_quantity),
                    "known_at_ms": ev.known_at_ms,
                }
                prior = state.get("funding_events", {}).get(ev.funding_id)
                if prior is not None:
                    if prior != body:
                        errors.append("FUNDING_ID_CONFLICT")
                    continue
                expected_qty = decimal(state["positions"].get(ev.market, "0"))
                if (
                    expected_qty == ZERO
                    or ev.position_quantity != expected_qty
                    or ev.known_at_ms > snapshot.observed_at_ms
                ):
                    errors.append("FUNDING_POSITION_MISMATCH")
                    continue
                try:
                    shares = apply_funding(state, body, expected_quantity=expected_qty)
                except ValueError:
                    errors.append("FUNDING_ATTRIBUTION_INVALID")
                    continue
                state.setdefault("funding_events", {})[ev.funding_id] = body
                new_funding = add(new_funding, ev.amount)
                for pod, share in shares.items():
                    used[pod] = str(add(decimal(used.get(pod, "0")), max(ZERO, -decimal(share))))
        known_orders: dict[str, BrokerOrder] = {}
        for order in snapshot.orders:
            if order.intent_id in known_orders and order != known_orders[order.intent_id]:
                errors.append("ORDER_ID_CONFLICT")
            known_orders[order.intent_id] = order
            intent = state["intents"].get(order.intent_id)
            if intent is None:
                errors.append("ORPHAN_ORDER")
                continue
            if (
                order.market != intent["market"]
                or order.quantity != decimal(intent["quantity"])
                or order.filled != decimal(intent["filled"])
                or order.reduce_only != intent["reduce_only"]
            ):
                errors.append("ORDER_FILL_MISMATCH")
                continue
            # A provider terminal label is not independent proof that its
            # cumulative executions filled the requested signed quantity.
            # Preserve the reservation and latch new risk on contradiction.
            if order.status == "FILLED" and order.filled != order.quantity:
                errors.append("BROKER_FALSE_FILLED_TERMINAL")
                continue
            if order.status in {"REJECTED", "NOT_ACCEPTED"} and order.filled != ZERO:
                errors.append("BROKER_TERMINAL_EXECUTION_CONFLICT")
                continue
            if order.status not in ALLOWED_BROKER_ORDER_STATUSES:
                errors.append("BROKER_STATUS_UNKNOWN")
                continue
            if order.status == "UNKNOWN":
                errors.append("BROKER_ORDER_UNKNOWN")
                continue
            intent.update(status=order.status, broker_id=order.broker_id)
        for key, intent in state["intents"].items():
            if key not in known_orders and intent["status"] not in TERMINAL:
                if intent["status"] == "QUEUED":
                    # A restart revokes an unsent intent; no catch-up.
                    if intent["boot_epoch"] != state["runtime"]["boot_epoch"]:
                        intent["status"] = "REVOKED"
                    elif (
                        intent.get("checkpoint_policy") == CHECKPOINT_POLICY_V3
                        and now_ms >= intent["expires_at_ms"]
                    ):
                        intent["status"] = "EXPIRED"
                elif snapshot.observed_at_ms >= intent.get("sent_at_ms", intent["created_at_ms"]):
                    # Only a complete genesis-to-cutoff history proves absence.
                    if decimal(intent["filled"]) == 0:
                        intent["status"] = "NOT_ACCEPTED"
                    else:
                        errors.append("MISSING_ORDER_HISTORY")
        observed = {p.market: p.quantity for p in snapshot.positions if p.quantity}
        expected = {m: decimal(q) for m, q in state["positions"].items() if decimal(q)}
        if observed != expected or len(observed) != len(snapshot.positions):
            errors.append("ORPHAN_POSITION")
        for market, basis in state.get("checkpoint_basis", {}).items():
            native = next((p for p in snapshot.positions if p.market == market), None)
            if decimal(basis["quantity"]) != (native.quantity if native else ZERO) or decimal(
                basis["entry_price"]
            ) != (native.entry_price if native else ZERO):
                errors.append("BROKER_ENTRY_BASIS_MISMATCH")
        old_cash = state["finance"].get("cash")
        if old_cash is not None and snapshot.cash is not None:
            # Only actual fills and verified funding flows with retained,
            # independently checked basis explain checkpoint realized cash.
            if snapshot.cash - decimal(old_cash) != add(new_realized, -new_fees, new_funding) and (
                "POD_REALIZED_ATTRIBUTION_UNVERIFIED" not in errors
            ):
                errors.append("BROKER_CASH_ATTRIBUTION_UNKNOWN")
        finance, finance_errors = account_step(state["finance"], snapshot, self.rules, now_ms)
        previous_day = state["finance"].get("day")
        state["finance"] = finance
        errors.extend(finance_errors)
        try:
            limits = {
                owner: loss_limit(state, owner, self.policy)
                if self.release.payload["schema"] == PORTFOLIO_RELEASE_V2
                else decimal(self.policy["pod_loss_budget"])
                for owner in used
            }
            if any(decimal(amount) >= limits[owner] for owner, amount in used.items()):
                errors.append("POD_DAILY_STOP")
        except ValueError as exc:
            errors.append(str(exc))
        if previous_day is not None and finance.get("day") != previous_day:
            state["pod_loss_used"] = {pod: "0" for pod in used}
        previous_marks.update({m: str(price) for m, price in marks.items()})
        if errors:
            for reason in errors:
                _halt(state, reason)
        else:
            for reservation, value in list(state["reservations"].items()):
                if all(state["intents"][key]["status"] in TERMINAL for key in value["intent_ids"]):
                    del state["reservations"][reservation]
            state["runtime"].update(
                certainty="COHERENT",
                last_snapshot_ms=snapshot.observed_at_ms,
                provider_cutoff=snapshot.cutoff,
                state="HALTED" if state["halts"] else "DISARMED",
            )
        flat = (
            not observed
            and (
                self.release.payload["schema"] != PORTFOLIO_RELEASE_V2
                or not any(
                    decimal(q) for book in state["pod_positions"].values() for q in book.values()
                )
            )
            and not state["reservations"]
            and not errors
            and all(i["status"] in TERMINAL for i in state["intents"].values())
        )
        state["runtime"]["flat_confirmed"] = flat
