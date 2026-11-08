"""The single account financial writer and sole synthetic dispatch authority."""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import DecisionReceipt, TargetBatchProposal
from .evidence import canonical, decimal, digest
from .finance import Rulebook, account_step
from .gateway import BrokerGateway, BrokerOrder, BrokerSnapshot
from .journal import Journal, State, StorageUnavailable
from .planner import compile_pods
from .release import ReleaseFloor, VerifiedRelease
from .risk import size_book
from .v4_timing import V4ShadowProjection

TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "NOT_ACCEPTED", "REVOKED"}
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
    ):
        self.journal, self.release, self.floor = journal, release, floor
        self.source_key, self.rules = source_key, rules
        self.policy, self.markets, self.gateway = policy, markets, gateway
        self.fault: Callable[[str], None] | None = None
        if journal.snapshot()["account_binding"] != release.payload["account_binding"]:
            raise ValueError("ACCOUNT_BINDING")
        if (
            digest({"policy": policy, "markets": markets}) != release.payload["risk_policy_sha"]
            or rules.sha != release.payload["rulebook_sha"]
        ):
            raise ValueError("POLICY_BINDING")
        floor.require_active(release)
        active = journal.snapshot().get("active_release_sha")
        if active != release.release_sha:

            def bind(s: State) -> None:
                if active is not None:
                    _halt(s, "RELEASE_CHANGED_REQUIRES_REVIEW")
                s["active_release_sha"] = release.release_sha
                s["runtime"]["permit"] = None

            journal.update("BIND_RELEASE_DISARMED", 0, bind)

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

    def decide(
        self, proposal: TargetBatchProposal, quotes: dict[str, Any], now_ms: int
    ) -> DecisionReceipt:
        proposal = TargetBatchProposal.from_dict(proposal.data)
        p = proposal.data
        state = self.journal.snapshot()
        existing = state["decisions"].get(p["proposal_id"])
        if existing:
            if existing["proposal_sha"] != digest(p):
                raise ValueError("PROPOSAL_PAYLOAD_CONFLICT")
            return DecisionReceipt.from_dict(existing["receipt"])
        reasons = self._scope(now_ms)
        status = "BLOCKED"
        if (
            p["release_sha"] != self.release.release_sha
            or p["account_binding"] != state["account_binding"]
        ):
            reasons.append("RELEASE_OR_ACCOUNT_SCOPE")
        if p["broker_epoch"] != state["runtime"]["broker_epoch"]:
            reasons.append("BROKER_EPOCH")
        if (
            p["decision_ms"] > now_ms
            or now_ms - p["decision_ms"] > 5000
            or not any(r["local_known_ms"] == p["first_known_ms"] for r in p["source_receipts"])
        ):
            reasons.append("CAUSAL_CLOCK")
        if p["quote_sha"] != digest(quotes):
            reasons.append("QUOTE_BINDING")
        for market, quote in quotes.items():
            try:
                mark = decimal(state.get("last_marks", {})[market])
                if not decimal(quote["bid"]) <= mark <= decimal(quote["ask"]):
                    raise ValueError("QUOTE_NOT_BROKER_MARK_BOUND")
                if decimal(quote["ask"]) / decimal(quote["bid"]) > Decimal("1.01"):
                    raise ValueError("SPREAD_OUTSIDE_ENVELOPE")
            except (KeyError, ValueError, TypeError, ZeroDivisionError):
                reasons.append("INDEPENDENT_MARKET_EVIDENCE_MISMATCH")
        if state["halts"]:
            reasons.extend(state["halts"])
        if state["runtime"]["certainty"] != "COHERENT":
            reasons.append("RECONCILIATION_UNCERTAIN")
        if (
            state["runtime"].get("last_snapshot_ms") is None
            or not 0 <= now_ms - state["runtime"]["last_snapshot_ms"] <= 5000
        ):
            reasons.append("BROKER_STALE")
        if state["reservations"] or any(
            i["status"] not in TERMINAL for i in state["intents"].values()
        ):
            reasons.append("PENDING_RESERVATION")
        try:
            expected = compile_pods(
                p["source_receipts"], self.release, self.source_key, p["decision_ms"]
            )
            if canonical(expected) != canonical(p["pods"]):
                raise ValueError("SOURCE_REQUEST_MISMATCH")
            for receipt in p["source_receipts"]:
                b = receipt["envelope"]["payload"]
                cursor = state["source_cursors"].get(b["source_identity"], -1)
                if b["sequence"] < cursor:
                    raise ValueError("SOURCE_CURSOR_ROLLBACK")
        except (ValueError, KeyError, TypeError) as exc:
            reasons.append(str(exc) if isinstance(exc, ValueError) else "SOURCE_INVALID")
            status = "REJECTED"
        risk: dict[str, Any] = {}
        if not reasons:
            pending = [
                {
                    "market": i["market"],
                    "remaining": str(decimal(i["quantity"]) - decimal(i["filled"])),
                }
                for i in state["intents"].values()
                if i["status"] not in TERMINAL
            ]
            risk = size_book(
                p["pods"],
                quotes,
                self.markets,
                self.policy,
                dict(state["finance"], pod_loss_used=state.get("pod_loss_used", {})),
                state["positions"],
                state["pod_positions"],
                pending,
                now_ms=now_ms,
            )
            status = "PREVIEW" if risk["status"] == "ACCEPTED" else risk["status"]
            reasons.extend(risk["reasons"])
        body: dict[str, Any] = {
            "schema": "guardian_decision_v1",
            "proposal_id": p["proposal_id"],
            "release_sha": self.release.release_sha,
            "status": status,
            "reasons": sorted(set(reasons)),
            "risk": risk,
            "known_at_ms": now_ms,
            "journal_epoch": state["runtime"]["boot_epoch"],
            "broker_epoch": state["runtime"]["broker_epoch"],
            "quote_sha": p["quote_sha"],
            "scenario_sha": digest(self.policy["scenarios"]),
            "rulebook_sha": self.rules.sha,
            "requested_pods": p["pods"],
            "trading_authorized": False,
        }
        body["decision_id"] = digest(body)

        def record(s: State) -> None:
            s["decisions"][p["proposal_id"]] = {
                "proposal_sha": digest(p),
                "receipt": body,
                "proposal": p,
                "quotes": quotes,
                "decision_sequence": self.journal.sequence + 1,
                "intent_ids": [],
            }

        self.journal.update("DECISION", now_ms, record)
        return DecisionReceipt.from_dict(body)

    def preview_v4(
        self, projection: V4ShadowProjection, action_id: str, quotes: dict[str, Any], now_ms: int
    ) -> DecisionReceipt:
        """Protected SHADOW review of authenticated V4 source eligibility.

        Must run with the Guardian-owned projection, never on Planner-submitted
        target quantities. The receipt deliberately cannot be reserved or
        dispatched: only the separately authorized production path may do so.
        """
        if (
            projection.release.release_sha != self.release.release_sha
            or projection.source_key.public_bytes_raw() != self.source_key.public_bytes_raw()
            or self.release.payload["source_mode"] != "PROSPECTIVE"
        ):
            raise ValueError("V4_PROJECTION_AUTHORITY")
        action = projection.current_action(action_id, now_ms)
        if action["trading_authorized"] is not False or action["due_ms"] > now_ms:
            raise ValueError("V4_ACTION_NOT_ELIGIBLE")
        proposal_id = digest(
            {
                "schema": "v4_shadow_proposal_v1",
                "release_sha": self.release.release_sha,
                "source_action_id": action_id,
            }
        )
        state = self.journal.snapshot()
        previous = state["decisions"].get(proposal_id)
        if previous is not None:
            return DecisionReceipt.from_dict(previous["receipt"])
        reasons = self._scope(now_ms)
        if state["halts"]:
            reasons.extend(state["halts"])
        if state["runtime"]["certainty"] != "COHERENT":
            reasons.append("RECONCILIATION_UNCERTAIN")
        if (
            state["runtime"].get("last_snapshot_ms") is None
            or not 0 <= now_ms - state["runtime"]["last_snapshot_ms"] <= 5000
        ):
            reasons.append("BROKER_STALE")
        if state["reservations"] or any(
            intent["status"] not in TERMINAL for intent in state["intents"].values()
        ):
            reasons.append("PENDING_RESERVATION")
        try:
            from .planner import validate_quotes

            validate_quotes(quotes, now_ms)
            for market, quote in quotes.items():
                mark = decimal(state.get("last_marks", {})[market])
                if not decimal(quote["bid"]) <= mark <= decimal(quote["ask"]) or decimal(
                    quote["ask"]
                ) / decimal(quote["bid"]) > Decimal("1.01"):
                    raise ValueError("INDEPENDENT_MARKET_EVIDENCE_MISMATCH")
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            reasons.append("INDEPENDENT_MARKET_EVIDENCE_MISMATCH")
        pods = []
        try:
            pods = projection.eligible_pods(now_ms)
        except (ValueError, KeyError, TypeError) as exc:
            reasons.append(str(exc) if isinstance(exc, ValueError) else "V4_SOURCE_UNAVAILABLE")
        risk: dict[str, Any] = {}
        if not reasons:
            pending = [
                {
                    "market": intent["market"],
                    "remaining": str(decimal(intent["quantity"]) - decimal(intent["filled"])),
                }
                for intent in state["intents"].values()
                if intent["status"] not in TERMINAL
            ]
            risk = size_book(
                pods,
                quotes,
                self.markets,
                self.policy,
                dict(state["finance"], pod_loss_used=state.get("pod_loss_used", {})),
                state["positions"],
                state["pod_positions"],
                pending,
                now_ms=now_ms,
            )
            if risk["status"] != "ACCEPTED":
                reasons.extend(risk.get("reasons", ["RISK_BLOCKED"]))
        status = "SHADOW_PREVIEW" if not reasons else "BLOCKED"
        receipt = {
            "schema": "guardian_v4_shadow_decision_v1",
            "proposal_id": proposal_id,
            "release_sha": self.release.release_sha,
            "source_action_id": action_id,
            "status": status,
            "reasons": sorted(set(reasons)),
            "risk": risk,
            "known_at_ms": now_ms,
            "journal_epoch": state["runtime"]["boot_epoch"],
            "broker_epoch": state["runtime"]["broker_epoch"],
            "quote_sha": digest(quotes),
            "requested_pods": pods,
            "trading_authorized": False,
            "broker_mutations": 0,
        }
        receipt["decision_id"] = digest(receipt)

        def record(s: State) -> None:
            s["decisions"][proposal_id] = {
                "proposal_sha": digest(action),
                "receipt": receipt,
                "proposal": None,
                "quotes": quotes,
                "decision_sequence": self.journal.sequence + 1,
                "intent_ids": [],
            }

        self.journal.update("V4_SHADOW_DECISION", now_ms, record)
        return DecisionReceipt.from_dict(receipt)

    def preview_attested_v4(
        self,
        projection: V4ShadowProjection,
        action_id: str,
        quote_envelope: dict[str, Any],
        quote_key: Ed25519PublicKey,
        catalog: frozenset[str],
        now_ms: int,
    ) -> DecisionReceipt:
        """Source T+300 with independently signed follower bid/ask.

        This path is source-only SHADOW and can NEVER reserve or dispatch.
        Missing provider quote provenance fails before any financial journal
        change; the source-observer signer is not an independent quote key.
        """
        from .v4_market_identity import validate_independent_quotes

        quotes = validate_independent_quotes(
            quote_envelope,
            quote_key=quote_key,
            source_key=self.source_key,
            release=self.release,
            catalog=catalog,
            now_ms=now_ms,
        )
        return self.preview_v4(projection, action_id, quotes, now_ms)

    def reserve(self, decision_id: str, now_ms: int) -> list[str]:
        state = self.journal.snapshot()
        record = next(
            (d for d in state["decisions"].values() if d["receipt"]["decision_id"] == decision_id),
            None,
        )
        if record is None:
            raise ValueError("DECISION_UNKNOWN")
        if record.get("reservation_committed"):
            return list(record["intent_ids"])
        receipt = record["receipt"]
        reasons = self._scope(now_ms)
        if (
            reasons
            or receipt["release_sha"] != self.release.release_sha
            or receipt["status"] != "PREVIEW"
            or state["halts"]
            or state["runtime"]["certainty"] != "COHERENT"
            or receipt["broker_epoch"] != state["runtime"]["broker_epoch"]
            or receipt["journal_epoch"] != state["runtime"]["boot_epoch"]
            or record["decision_sequence"] != self.journal.sequence
            or not 0 <= now_ms - receipt["known_at_ms"] <= 5000
            or state["reservations"]
        ):
            raise ValueError("RESERVATION_NOT_PERMITTED")
        # This binary has no live authorizer. Authenticated remote gateways are read-only.
        if (
            self.gateway.synthetic is not True
            or self.release.payload["source_mode"] != "SYNTHETIC"
            or self.rules.status != "SYNTHETIC"
            or state["account_binding"] != "fixture-account"
        ):
            raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")
        risk = receipt["risk"]
        intents = []
        for market in sorted(set(risk["targets"]) | set(state["positions"])):
            quantity = decimal(risk["targets"].get(market, "0")) - decimal(
                state["positions"].get(market, "0")
            )
            if not quantity:
                continue
            quote = record["quotes"][market]
            slip = decimal(self.policy["shortfall_bps"]) / 10000
            price = (
                decimal(quote["ask"]) * (1 + slip)
                if quantity > 0
                else decimal(quote["bid"]) * (1 - slip)
            )
            tick = decimal(self.markets[market]["price_tick"])
            price = (price / tick).to_integral_value(
                rounding=ROUND_FLOOR if quantity > 0 else ROUND_CEILING
            ) * tick
            intent_id = digest(
                {
                    "account": state["account_binding"],
                    "decision": decision_id,
                    "market": market,
                    "quantity": str(quantity),
                }
            )
            changes = {
                pod: str(
                    decimal(target.get(market, "0"))
                    - decimal(state["pod_positions"].get(pod, {}).get(market, "0"))
                )
                for pod, target in risk["pod_targets"].items()
            }
            intents.append(
                {
                    "intent_id": intent_id,
                    "account_binding": state["account_binding"],
                    "endpoint": "fake://broker",
                    "decision_id": decision_id,
                    "release_sha": self.release.release_sha,
                    "market": market,
                    "quantity": str(quantity),
                    "limit_price": str(price),
                    "reduce_only": False,
                    "status": "QUEUED",
                    "filled": "0",
                    "broker_id": None,
                    "broker_epoch": receipt["broker_epoch"],
                    "boot_epoch": receipt["journal_epoch"],
                    "expires_at_ms": receipt["known_at_ms"] + 5000,
                    "created_at_ms": now_ms,
                    "pod_changes": changes,
                    "proof_sha": digest(receipt),
                    "reservation_id": decision_id,
                }
            )
        ids = [i["intent_id"] for i in intents]
        # Exact opposing pod targets can cross internally only in the synthetic fixture.
        # There is no external execution, cash movement, fee, or broker position.
        # A nonzero broker delta must never take this path: its pod attribution is
        # determined by independently observed broker fills instead.
        internal_cross: dict[str, dict[str, str]] = {}
        if not ids:
            for pod, book in risk["pod_targets"].items():
                delta = {
                    market: str(
                        decimal(quantity)
                        - decimal(state["pod_positions"].get(pod, {}).get(market, "0"))
                    )
                    for market, quantity in book.items()
                }
                for market, quantity in state["pod_positions"].get(pod, {}).items():
                    if market not in delta:
                        delta[market] = str(-decimal(quantity))
                if any(decimal(quantity) for quantity in delta.values()):
                    internal_cross[pod] = delta
            if any(
                sum((decimal(book.get(market, "0")) for book in internal_cross.values()), ZERO)
                for market in {m for book in internal_cross.values() for m in book}
            ):
                raise ValueError("INTERNAL_CROSS_NOT_NET_ZERO")

        def commit(s: State) -> None:
            if internal_cross:
                for pod, deltas in internal_cross.items():
                    book = s["pod_positions"].setdefault(pod, {})
                    for market, quantity in deltas.items():
                        book[market] = str(decimal(book.get(market, "0")) + decimal(quantity))
                s.setdefault("internal_allocations", []).append(
                    {
                        "decision_id": decision_id,
                        "known_at_ms": now_ms,
                        "pod_deltas": internal_cross,
                        "scope": "SYNTHETIC_ZERO_BROKER_NET",
                    }
                )
            if ids:
                s["reservations"][decision_id] = {
                    "loss": risk["worst_loss"],
                    "margin": risk["stressed_margin"],
                    "cost": risk["cost_reserve"],
                    "intent_ids": ids,
                }
            for intent in intents:
                s["intents"][intent["intent_id"]] = intent
            for item in record["proposal"]["source_receipts"]:
                b = item["envelope"]["payload"]
                s["source_cursors"][b["source_identity"]] = max(
                    s["source_cursors"].get(b["source_identity"], -1), b["sequence"]
                )
            s["decisions"][receipt["proposal_id"]]["intent_ids"] = ids
            s["decisions"][receipt["proposal_id"]]["reservation_committed"] = True
            s["runtime"].update(
                permit={
                    "decision_id": decision_id,
                    "boot_epoch": receipt["journal_epoch"],
                    "broker_epoch": receipt["broker_epoch"],
                    "expires_at_ms": receipt["known_at_ms"] + 5000,
                },
                state="SHADOW_READY",
            )

        self._boundary("before_reservation")
        self.journal.update("RESERVE_AND_OUTBOX", now_ms, commit)
        self._boundary("after_reservation")
        return ids

    def dispatch(self, intent_id: str, now_ms: int) -> None:
        state = self.journal.snapshot()
        intent = state["intents"][intent_id]
        permit = state["runtime"]["permit"]
        if (
            self._scope(now_ms)
            or intent["release_sha"] != self.release.release_sha
            or state["halts"]
            or intent["status"] != "QUEUED"
            or not permit
            or self.gateway.synthetic is not True
            or intent["boot_epoch"] != state["runtime"]["boot_epoch"]
            or intent["broker_epoch"] != state["runtime"]["broker_epoch"]
            or permit["decision_id"] != intent["decision_id"]
            or now_ms > intent["expires_at_ms"]
            or state["runtime"]["certainty"] != "COHERENT"
            or not 0 <= now_ms - state["runtime"]["last_snapshot_ms"] <= 5000
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
            if outcome.status not in {"ACCEPTED", "FILLED", "PARTIAL", "REJECTED"}:
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
        if self.gateway.synthetic is not True or intent_id not in state["intents"]:
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
        for fill in sorted(snapshot.fills, key=lambda f: (f.known_at_ms, f.execution_id)):
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
            total = decimal(intent["filled"]) + fill.quantity
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
            if decimal(state["positions"].get(fill.market, "0")) * fill.quantity < 0:
                # Realized PnL needs independently confirmed pod entry basis.
                errors.append("POD_REALIZED_ATTRIBUTION_UNVERIFIED")
            new_fees += fill.fee
            intent["filled"] = str(total)
            state["fills"][fill.execution_id] = body
            state["positions"][fill.market] = str(
                decimal(state["positions"].get(fill.market, "0")) + fill.quantity
            )
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
            intent.update(status=order.status, broker_id=order.broker_id)
        for key, intent in state["intents"].items():
            if key not in known_orders and intent["status"] not in TERMINAL:
                if intent["status"] == "QUEUED":
                    # A restart revokes an unsent intent; no catch-up.
                    if intent["boot_epoch"] != state["runtime"]["boot_epoch"]:
                        intent["status"] = "REVOKED"
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
        old_cash = state["finance"].get("cash")
        if old_cash is not None and snapshot.cash is not None:
            # New fills explain fees only when there is no realized close. Other
            # debits/credits (funding, corrections, transfers) require explicit
            # typed provider evidence before assigning any pod economic effect.
            if snapshot.cash - decimal(old_cash) != -new_fees and (
                "POD_REALIZED_ATTRIBUTION_UNVERIFIED" not in errors
            ):
                errors.append("BROKER_CASH_ATTRIBUTION_UNKNOWN")
        finance, finance_errors = account_step(state["finance"], snapshot, self.rules, now_ms)
        previous_day = state["finance"].get("day")
        state["finance"] = finance
        errors.extend(finance_errors)
        if any(
            decimal(amount) >= decimal(self.policy["pod_loss_budget"]) for amount in used.values()
        ):
            errors.append("POD_DAILY_STOP")
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
            and not state["reservations"]
            and not errors
            and all(i["status"] in TERMINAL for i in state["intents"].values())
        )
        state["runtime"]["flat_confirmed"] = flat
