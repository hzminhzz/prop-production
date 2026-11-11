"""Checkpoint admission policy inside the existing Guardian and financial journal."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .checkpoint_opportunity import OpportunityEvent as Event
from .checkpoint_opportunity import OpportunityPhase as Phase
from .checkpoint_opportunity import OpportunityState, advance
from .checkpoint_source import integer, verify_transition
from .copy_mandates import CopyMandate
from .evidence import decimal, digest, load_json, verify
from .gateway import execution_capability
from .guardian_checkpoints import _authority, _source
from .planner import validate_quotes
from .risk import size_book

if TYPE_CHECKING:
    from .guardian import Guardian
    from .v4_source_receiver import SourcePacketReceiver

ZERO = Decimal(0)
TERMINAL = {Phase.COMMITTED_ONCE, Phase.MISSED}
POLICY = "CWSA_CHECKPOINT_V3"


def execution_policy(guardian: Guardian) -> dict[str, Any]:
    cfg = guardian.policy.get("checkpoint_execution")
    if not isinstance(cfg, dict) or not {
        "quote_max_age_ms",
        "account_max_age_ms",
        "max_gross_notional",
    } <= set(cfg):
        raise ValueError("CHECKPOINT_EXECUTION_POLICY_UNQUALIFIED")
    if (
        any(not 0 < integer(cfg[k]) <= 5000 for k in ("quote_max_age_ms", "account_max_age_ms"))
        or decimal(cfg["max_gross_notional"]) <= 0
    ):
        raise ValueError("CHECKPOINT_EXECUTION_POLICY_INVALID")
    return cfg


def account_anchor(state: dict[str, Any]) -> str:
    return digest(
        {
            k: state[k]
            for k in (
                "account_binding",
                "active_release_sha",
                "finance",
                "positions",
                "pod_positions",
                "reservations",
                "halts",
            )
        }
        | {
            k: state.get(k, {})
            for k in ("pod_loss_used", "pod_fees_paid", "pod_realized", "checkpoint_basis")
        }
        | {
            "runtime": {
                k: state["runtime"].get(k)
                for k in ("boot_epoch", "broker_epoch", "last_snapshot_ms", "certainty")
            }
        }
    )


def source_proof(
    envelope: dict[str, Any],
    public: str,
    outcome_id: str,
    scope: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public))
    body = verify(envelope, key, "cwsa-checkpoint-v3")
    if (
        body["scope"] != scope
        or body["policy"] != policy
        or body["schema"] != "cwsa_checkpoint_packet_v3"
        or body["trading_authorized"] is not False
    ):
        raise ValueError("CHECKPOINT_SOURCE_POLICY_BINDING")
    from hashlib import sha256

    if any(
        sha256(raw.encode()).hexdigest() != raw_sha for raw_sha, raw in body["originals"].items()
    ):
        raise ValueError("CHECKPOINT_NATIVE_ORIGINAL_HASH")
    selected = None
    for event in body["transitions"]:
        verify_transition(scope, policy, event, body["originals"], issued_ms=body["issued_ms"])
        if event["effect"] is not None and event["effect"]["outcome_id"] == outcome_id:
            selected = event
    if (
        selected is None
        or selected["effect"]["kind"] != "CHANGED"
        or selected["predecessor"]["phase"] != "CONTINUOUS"
    ):
        raise ValueError("CHECKPOINT_CHANGED_PROOF_REQUIRED")
    outcome = selected["effect"]
    marks = {}
    for receipt in outcome["sample"]["receipts"]:
        native = load_json(body["originals"][receipt["raw_sha"]])
        dex = receipt["dex"]
        for row in native["assetPositions"]:
            leg = row["position"]
            quantity = decimal(leg["szi"])
            if not quantity:
                continue
            if leg.get("positionValue") is None:
                continue
            value = decimal(leg["positionValue"])
            if value <= 0:
                raise ValueError("CHECKPOINT_NATIVE_MARK")
            name = leg["coin"] if dex == "default" else leg["coin"][len(dex) + 1 :]
            marks[f"hyperliquid/mainnet:perp:{dex}/{name}"] = str(value / abs(quantity))
    return dict(outcome), marks


def _move(
    record: dict[str, Any], event: Event, *, now_ms: int, evidence_sha: str, guard: str
) -> None:
    before = OpportunityState.parse(record["state"])
    transition = advance(
        before,
        event=event,
        evidence_sha=evidence_sha,
        at_ms=now_ms,
        guard=guard,
        no_side_effect=event in (Event.REVIEW, Event.BLOCK),
    )
    record["history"].append(transition.data)
    record["state"] = transition.successor.data


def _prerequisites(
    guardian: Guardian,
    state: dict[str, Any],
    quotes: dict[str, Any],
    cfg: dict[str, Any],
    now_ms: int,
    needed: set[str],
) -> tuple[list[str], dict[str, Any]]:
    reasons = []
    evidence: dict[str, Any] = {}
    if state["runtime"]["certainty"] != "COHERENT" or not state["finance"]:
        reasons.append("RECONCILIATION_REQUIRED")
    if not 0 <= now_ms - state["runtime"].get("last_snapshot_ms", 0) <= cfg["account_max_age_ms"]:
        reasons.append("ACCOUNT_FACTS_STALE")
    try:
        validate_quotes(quotes, now_ms)
        if not needed.issubset(quotes):
            raise ValueError("INDEPENDENT_QUOTE_MISSING")
        if any(now_ms - q["known_at_ms"] > cfg["quote_max_age_ms"] for q in quotes.values()):
            raise ValueError("INDEPENDENT_QUOTE_STALE")
        snapshot = guardian.gateway.observe(now_ms)
        if snapshot.account_binding != state["account_binding"]:
            raise ValueError("INDEPENDENT_ACCOUNT_BINDING")
        if not snapshot.ready(now_ms, cfg["account_max_age_ms"]):
            raise ValueError("INDEPENDENT_ACCOUNT_UNAVAILABLE")
        observed = {p.market: str(p.quantity) for p in snapshot.positions if p.quantity}
        expected = {m: q for m, q in state["positions"].items() if decimal(q)}
        if {m: decimal(q) for m, q in observed.items()} != {
            m: decimal(q) for m, q in expected.items()
        }:
            raise ValueError("INDEPENDENT_POSITION_RECONCILIATION")
        if state["finance"] and (
            str(snapshot.cash) != state["finance"]["cash"]
            or str(snapshot.equity) != state["finance"]["equity"]
        ):
            raise ValueError("INDEPENDENT_FINANCE_RECONCILIATION")
        capabilities = {raw["instrument"]: raw for raw in snapshot.execution_capabilities}
        if len(capabilities) != len(snapshot.execution_capabilities):
            raise ValueError("EXECUTION_CAPABILITY_AMBIGUOUS")
        for market in needed:
            if market not in capabilities or not execution_capability(
                capabilities[market],
                "net_order",
                account=state["account_binding"],
                endpoint=snapshot.endpoint,
                instrument=market,
                now_ms=now_ms,
                synthetic=guardian.gateway.synthetic,
            ):
                raise ValueError("EXECUTION_CAPABILITY_UNQUALIFIED")
        marks = {m: (price, known) for m, price, known in snapshot.market_marks}
        for market in needed:
            price, known = marks[market]
            q = quotes[market]
            if not 0 <= now_ms - known <= cfg["account_max_age_ms"] or not decimal(
                q["bid"]
            ) <= price <= decimal(q["ask"]):
                raise ValueError("INDEPENDENT_QUOTE_MARK_MISMATCH")
            if (decimal(q["ask"]) - decimal(q["bid"])) / decimal(q["bid"]) > Decimal("0.01"):
                raise ValueError("INDEPENDENT_QUOTE_SPREAD")
        evidence = {
            "account_binding": snapshot.account_binding,
            "endpoint": snapshot.endpoint,
            "observed_at_ms": snapshot.observed_at_ms,
            "received_at_ms": snapshot.received_at_ms,
            "epoch": snapshot.epoch,
            "cash": str(snapshot.cash),
            "equity": str(snapshot.equity),
            "positions": observed,
            "cutoff": snapshot.cutoff,
            "market_marks": [[m, str(p), t] for m, p, t in snapshot.market_marks],
            "execution_capabilities": capabilities,
        }
    except (ValueError, KeyError, TimeoutError, ConnectionError, OSError) as exc:
        reasons.append(str(exc))
    return reasons, evidence


def review(
    guardian: Guardian,
    receiver: SourcePacketReceiver,
    requests: dict[str, str],
    *,
    quotes: dict[str, Any],
    now_ms: int,
    quote_envelope: dict[str, Any] | None = None,
    quote_key: Ed25519PublicKey | None = None,
) -> dict[str, Any]:
    _authority(guardian)
    _source(guardian, receiver)
    integer(now_ms)
    cfg = execution_policy(guardian)
    if not isinstance(requests, dict) or not requests:
        raise ValueError("CHECKPOINT_REQUESTS")
    state = guardian.journal.snapshot()
    p = guardian.release.payload
    source_rows = {
        r["outcome"]["outcome_id"]: r for r in receiver.checkpoint_outcomes(now_ms=now_ms)
    }
    opportunities = state.setdefault("checkpoint_opportunities", {})
    selected, dispositions = {}, {}
    assert receiver.checkpoint is not None
    for owner, outcome_id in sorted(requests.items()):
        entry = state.get("copy_mandates", {}).get(owner)
        row = source_rows.get(outcome_id)
        if entry is None or row is None:
            raise ValueError("CHECKPOINT_OWNER_OR_OUTCOME_UNKNOWN")
        m = CopyMandate.parse(entry["mandate"])
        outcome = row["outcome"]
        if (
            m.data not in p["mandates"]
            or outcome["source_id"] != m.data["source_id"]
            or outcome["scope_generation"] != m.data["source_scope_generation"]
            or outcome["policy_epoch"] != m.data["policy_epoch"]
        ):
            raise ValueError("CHECKPOINT_MANDATE_SCOPE")
        if outcome["kind"] != "CHANGED":
            dispositions[owner] = outcome["kind"]
            continue
        if entry["state"]["phase"] != "ACTIVE":
            dispositions[owner] = "MANDATE_" + entry["state"]["phase"]
            continue
        identity = digest(["checkpoint_opportunity_v3", owner, outcome["signal_id"]])
        deadline = min(row["deadline_ms"], m.data["effective_until_ms"])
        proof = receiver.checkpoint.packet_for(outcome_id)
        selected[identity] = (m, row, entry)
        opportunities.setdefault(
            identity,
            {
                "state": OpportunityState(identity, owner, outcome["signal_id"], deadline).data,
                "history": [],
                "proof": proof,
                "source_public": guardian.source_key.public_bytes_raw().hex(),
                "scope": p["checkpoint_scopes"][outcome["source_id"]],
                "policy": p["checkpoint_policy"],
                "outcome_id": outcome_id,
                "mandate_sha": m.sha,
                "commitment": None,
            },
        )
    if not selected:
        return {
            "status": "SOURCE_ONLY",
            "source_dispositions": dispositions,
            "intent_ids": [],
            "order_route": "NONE",
            "trading_authorized": False,
        }
    boundaries = {row["outcome"]["checkpoint_ms"] for _, row, _ in selected.values()}
    if len(boundaries) != 1:
        raise ValueError("INCOMPATIBLE_CHECKPOINT_BATCH")
    checkpoint_ms = next(iter(boundaries))
    decision_id = digest(
        [
            "checkpoint_batch_v3",
            state["account_binding"],
            guardian.release.release_sha,
            sorted(selected),
        ]
    )
    decisions = state.setdefault("checkpoint_decisions", {})
    previous = decisions.get(decision_id)
    if previous and previous["result"]["status"] in TERMINAL:
        return dict(previous["result"])
    deadline = min(opportunities[key]["state"]["deadline_ms"] for key in selected)
    result: dict[str, Any] = {
        "decision_id": decision_id,
        "checkpoint_ms": checkpoint_ms,
        "status": "REVIEWABLE",
        "deadline_ms": deadline,
        "intent_ids": [],
        "risk": None,
        "reasons": [],
        "source_dispositions": dispositions,
        "source_signal_ids": sorted({opportunities[k]["state"]["signal_id"] for k in selected}),
        "trading_authorized": False,
    }
    terminal_reasons = list(guardian._scope(now_ms))
    if state["halts"]:
        terminal_reasons.append("PERSISTENT_HALT")
    if state["reservations"] or any(
        i["status"] in ("UNKNOWN", "DISPATCHING", "CANCEL_PENDING")
        for i in state["intents"].values()
    ):
        terminal_reasons.append("UNRESOLVED_FINANCIAL_OBLIGATIONS")
    if now_ms >= deadline:
        terminal_reasons.append("IMMUTABLE_DEADLINE_EXPIRED")
    if any(
        entry["state"]["phase"] != "ACTIVE" or not row["opening_candidate"]
        for _, row, entry in selected.values()
    ):
        terminal_reasons.append("INACTIVE_OR_SOURCE_NOT_ACTIONABLE")
    evidence_sha = digest([requests, quotes, now_ms, account_anchor(state), terminal_reasons])
    for identity in selected:
        record = opportunities[identity]
        if record["state"]["phase"] in TERMINAL:
            terminal_reasons.append("OPPORTUNITY_ALREADY_TERMINAL")
        elif terminal_reasons:
            _move(
                record, Event.MISS, now_ms=now_ms, evidence_sha=evidence_sha, guard="TERMINAL_GUARD"
            )
        else:
            if record["state"]["phase"] == Phase.REVIEWABLE:
                _move(
                    record,
                    Event.BLOCK,
                    now_ms=now_ms,
                    evidence_sha=evidence_sha,
                    guard="PREDISPATCH_REVALIDATION",
                )
            _move(
                record,
                Event.REVIEW,
                now_ms=now_ms,
                evidence_sha=evidence_sha,
                guard="COMPLETE_SOURCE_AND_MANDATE",
            )
    pods: list[dict[str, Any]] = []
    active_owners = {m.owner for m, _, _ in selected.values()}
    frozen = {
        owner: dict(book)
        for owner, book in state["pod_positions"].items()
        if owner not in active_owners
    }
    if not terminal_reasons:
        try:
            mapping = {
                spec["source_instrument"]: market
                for market, spec in guardian.markets.items()
                if "source_instrument" in spec
            }
            if len(mapping) != len(guardian.markets):
                raise ValueError("CHECKPOINT_NATIVE_MARKET_MAPPING")
            for identity, (m, _, _) in selected.items():
                record = opportunities[identity]
                outcome, marks = source_proof(
                    record["proof"],
                    record["source_public"],
                    record["outcome_id"],
                    record["scope"],
                    record["policy"],
                )
                if set(marks) != {leg["instrument"] for leg in outcome["positions"]}:
                    raise ValueError("CHECKPOINT_NATIVE_SOURCE_MARK_UNAVAILABLE")
                requested = {}
                for leg in outcome["positions"]:
                    source = leg["instrument"]
                    if source not in m.data["allowed_markets"] or source not in mapping:
                        raise ValueError("CHECKPOINT_NATIVE_MARKET_UNSUPPORTED")
                    requested[mapping[source]] = str(
                        decimal(leg["quantity"]) * decimal(marks[source]) * decimal(m.data["k"])
                    )
                pods.append({"pod_id": m.owner, "requested_notionals": requested})
            for owner in frozen:
                pods.append({"pod_id": owner, "requested_notionals": {}})
            pods.sort(key=lambda pod: pod["pod_id"])
            needed = set(state["positions"]) | {
                m for pod in pods for m in pod["requested_notionals"]
            }
            if p["source_mode"] != "SYNTHETIC":
                from hashlib import sha256

                from .v4_market_identity import validate_independent_quotes

                if (
                    quote_key is None
                    or quote_envelope is None
                    or sha256(quote_key.public_bytes_raw()).hexdigest()
                    != cfg.get("quote_authority_sha")
                ):
                    raise ValueError("INDEPENDENT_NATIVE_QUOTE_AUTHORITY_UNQUALIFIED")
                proved = validate_independent_quotes(
                    quote_envelope,
                    quote_key=quote_key,
                    source_key=guardian.source_key,
                    release=guardian.release,
                    catalog=frozenset(mapping),
                    now_ms=now_ms,
                )
                if quotes != {mapping[k]: value for k, value in proved.items()}:
                    raise ValueError("INDEPENDENT_NATIVE_QUOTE_BINDING")
            temporary, account_proof = _prerequisites(guardian, state, quotes, cfg, now_ms, needed)
            if temporary:
                result.update(status="PRE_DISPATCH_BLOCKED", reasons=temporary)
            else:
                finance = dict(state["finance"], pod_loss_used=state.get("pod_loss_used", {}))
                risk = size_book(
                    pods,
                    quotes,
                    guardian.markets,
                    guardian.policy,
                    finance,
                    state["positions"],
                    state["pod_positions"],
                    [],
                    now_ms=now_ms,
                    quantity_price_policy="EXECUTABLE_SIDE_V3",
                    frozen_pods=frozen,
                )
                result["risk"] = risk
                if risk["status"] != "ACCEPTED":
                    terminal_reasons.extend(risk["reasons"])
                else:
                    from .checkpoint_netting import conflicts

                    if conflicts(state, risk, checkpoint_ms):
                        terminal_reasons.append("MARKET_CHECKPOINT_ALREADY_COMMITTED")
        except (ValueError, KeyError) as exc:
            terminal_reasons.append(str(exc))
            account_proof = {}
    else:
        account_proof = {}
    if terminal_reasons:
        result.update(status="MISSED", reasons=sorted(set(terminal_reasons)))
    for identity in selected:
        record = opportunities[identity]
        if record["state"]["phase"] in TERMINAL:
            continue
        if result["status"] == "MISSED":
            _move(
                record,
                Event.MISS,
                now_ms=now_ms,
                evidence_sha=evidence_sha,
                guard="TERMINAL_POLICY_REJECTION",
            )
        elif result["status"] == "PRE_DISPATCH_BLOCKED":
            _move(
                record,
                Event.BLOCK,
                now_ms=now_ms,
                evidence_sha=evidence_sha,
                guard="PREREQUISITES_UNAVAILABLE_NO_DISPATCH",
            )
    attempt = {
        "at_ms": now_ms,
        "account_sha": account_anchor(state),
        "quotes": quotes,
        "policy": guardian.policy,
        "markets": guardian.markets,
        "account_proof": account_proof,
        "pods": pods,
        "frozen_pods": frozen,
        "risk": result["risk"],
        "result": result,
        "journal_sequence": guardian.journal.sequence + 1,
    }
    record = decisions.setdefault(
        decision_id,
        {
            "requests": dict(requests),
            "opportunity_ids": sorted(selected),
            "release_sha": guardian.release.release_sha,
            "attempts": [],
            "intent_ids": [],
        },
    )
    record["attempts"].append(attempt)
    record["result"] = result
    guardian.journal.commit(
        "CHECKPOINT_REVIEW", now_ms, state, expected_sequence=guardian.journal.sequence
    )
    return dict(result)


def reserve(
    guardian: Guardian, decision_id: str, *, receiver: SourcePacketReceiver, now_ms: int
) -> list[str]:
    _authority(guardian)
    _source(guardian, receiver)
    state = guardian.journal.snapshot()
    record = state.get("checkpoint_decisions", {}).get(decision_id)
    if record is None:
        raise ValueError("CHECKPOINT_DECISION_UNKNOWN")
    if record["result"]["status"] == "COMMITTED_ONCE":
        return list(record["intent_ids"])
    if (
        guardian.gateway.synthetic is not True
        or guardian.release.payload["source_mode"] != "SYNTHETIC"
        or guardian.rules.status != "SYNTHETIC"
        or state["account_binding"] != "fixture-account"
    ):
        raise PermissionError("REMOTE_MUTATION_UNAVAILABLE")
    attempt = record["attempts"][-1]
    if now_ms >= record["result"]["deadline_ms"]:
        review(guardian, receiver, record["requests"], quotes=attempt["quotes"], now_ms=now_ms)
        raise ValueError("CHECKPOINT_RESERVATION_NOT_PERMITTED")
    cfg = execution_policy(guardian)
    if (
        record["result"]["status"] != "REVIEWABLE"
        or guardian._scope(now_ms)
        or state["halts"]
        or state["reservations"]
        or account_anchor(state) != attempt["account_sha"]
        or guardian.journal.sequence != attempt["journal_sequence"]
        or now_ms >= record["result"]["deadline_ms"]
    ):
        raise ValueError("CHECKPOINT_RESERVATION_NOT_PERMITTED")
    current = {r["outcome"]["outcome_id"]: r for r in receiver.checkpoint_outcomes(now_ms=now_ms)}
    if any(
        not current.get(state["checkpoint_opportunities"][identity]["outcome_id"], {}).get(
            "opening_candidate"
        )
        or state["copy_mandates"][state["checkpoint_opportunities"][identity]["state"]["owner"]][
            "state"
        ]["phase"]
        != "ACTIVE"
        for identity in record["opportunity_ids"]
    ):
        raise ValueError("CHECKPOINT_RESERVATION_SOURCE_OR_MANDATE")
    risk = attempt["risk"]
    reasons, _ = _prerequisites(
        guardian,
        state,
        attempt["quotes"],
        cfg,
        now_ms,
        set(risk["targets"]) | set(state["positions"]),
    )
    if reasons:
        raise ValueError("CHECKPOINT_RESERVATION_PREREQUISITES:" + ",".join(reasons))
    intents = []
    for market in sorted(set(risk["targets"]) | set(state["positions"])):
        old, target = (
            decimal(state["positions"].get(market, "0")),
            decimal(risk["targets"].get(market, "0")),
        )
        quantity = target - old
        if not quantity:
            continue
        if old * target < 0:
            raise ValueError("CHECKPOINT_REVERSAL_CAPABILITY_REQUIRED")
        quote = attempt["quotes"][market]
        slip = decimal(guardian.policy["shortfall_bps"]) / 10000
        price = (
            decimal(quote["ask"]) * (1 + slip)
            if quantity > 0
            else decimal(quote["bid"]) * (1 - slip)
        )
        tick = decimal(guardian.markets[market]["price_tick"])
        price = (price / tick).to_integral_value(
            rounding=ROUND_FLOOR if quantity > 0 else ROUND_CEILING
        ) * tick
        identity = digest(
            {
                "account": state["account_binding"],
                "decision": decision_id,
                "market": market,
                "quantity": str(quantity),
            }
        )
        changes = {
            owner: str(
                decimal(book.get(market, "0"))
                - decimal(state["pod_positions"].get(owner, {}).get(market, "0"))
            )
            for owner, book in risk["pod_targets"].items()
        }
        intents.append(
            {
                "intent_id": identity,
                "account_binding": state["account_binding"],
                "endpoint": "fake://broker",
                "decision_id": decision_id,
                "release_sha": guardian.release.release_sha,
                "market": market,
                "quantity": str(quantity),
                "limit_price": str(price),
                "reduce_only": bool(old and old * quantity < 0 and abs(quantity) <= abs(old)),
                "status": "QUEUED",
                "filled": "0",
                "broker_id": None,
                "broker_epoch": state["runtime"]["broker_epoch"],
                "boot_epoch": state["runtime"]["boot_epoch"],
                "expires_at_ms": record["result"]["deadline_ms"],
                "created_at_ms": now_ms,
                "pod_changes": changes,
                "proof_sha": digest(attempt),
                "reservation_id": decision_id,
                "checkpoint_policy": POLICY,
                "opportunity_ids": record["opportunity_ids"],
                "account_max_age_ms": cfg["account_max_age_ms"],
            }
        )
    ids = [intent["intent_id"] for intent in intents]
    from .checkpoint_accounting import apply_internal
    from .checkpoint_netting import claims

    state.setdefault("checkpoint_market_claims", {}).update(
        claims(state, record, decision_id, now_ms)
    )
    apply_internal(state, record, decision_id, now_ms)
    for identity in record["opportunity_ids"]:
        opportunity = state["checkpoint_opportunities"][identity]
        _move(
            opportunity,
            Event.COMMIT,
            now_ms=now_ms,
            evidence_sha=digest(attempt),
            guard="RESERVE_BEFORE_SEND",
        )
        opportunity["commitment"] = decision_id
    if ids:
        state["reservations"][decision_id] = {
            "loss": risk["worst_loss"],
            "margin": risk["stressed_margin"],
            "cost": risk["cost_reserve"],
            "intent_ids": ids,
        }
    for intent in intents:
        state["intents"][intent["intent_id"]] = intent
    record["intent_ids"] = ids
    record["result"] = dict(record["result"], status="COMMITTED_ONCE", intent_ids=ids)
    state["runtime"].update(
        state="SHADOW_READY" if ids else "DISARMED",
        permit=(
            {
                "decision_id": decision_id,
                "boot_epoch": state["runtime"]["boot_epoch"],
                "broker_epoch": state["runtime"]["broker_epoch"],
                "expires_at_ms": record["result"]["deadline_ms"],
            }
            if ids
            else None
        ),
    )
    guardian._boundary("before_checkpoint_reservation")
    guardian.journal.commit(
        "CHECKPOINT_RESERVE", now_ms, state, expected_sequence=guardian.journal.sequence
    )
    guardian._boundary("after_checkpoint_reservation")
    return ids


def validate_history(previous: dict[str, Any], current: dict[str, Any], kind: str) -> None:
    from .checkpoint_accounting import validate_attribution
    from .checkpoint_netting import validate_claims

    validate_attribution(previous, current, kind)
    validate_claims(previous, current, kind)
    old = previous.get("checkpoint_opportunities", {})
    rows = current.get("checkpoint_opportunities", {})
    if not set(old).issubset(rows):
        raise ValueError("CHECKPOINT_OPPORTUNITY_REMOVED")
    for identity, row in rows.items():
        m = CopyMandate.parse(current["copy_mandates"][row["state"]["owner"]]["mandate"])
        outcome, _ = source_proof(
            row["proof"], row["source_public"], row["outcome_id"], row["scope"], row["policy"]
        )
        deadline = min(
            outcome["checkpoint_ms"] + 300000,
            m.data["effective_until_ms"],
            min(r["state_ms"] for r in outcome["sample"]["receipts"])
            + row["policy"]["limits"]["max_state_age_ms"],
        )
        before = OpportunityState(identity, m.owner, outcome["signal_id"], deadline)
        if (
            identity != digest(["checkpoint_opportunity_v3", m.owner, outcome["signal_id"]])
            or row["mandate_sha"] != m.sha
        ):
            raise ValueError("CHECKPOINT_OPPORTUNITY_IDENTITY")
        for raw in row["history"]:
            transition = advance(
                before,
                event=Event(raw["event"]),
                evidence_sha=raw["evidence_sha"],
                at_ms=raw["at_ms"],
                guard=raw["guard"],
                no_side_effect=raw["event"] in ("REVIEW", "BLOCK"),
            )
            if raw != transition.data:
                raise ValueError("CHECKPOINT_OPPORTUNITY_REPLAY")
            before = transition.successor
        if row["state"] != before.data:
            raise ValueError("CHECKPOINT_OPPORTUNITY_HEAD")
        if identity in old:
            prior = old[identity]
            if row["history"][: len(prior["history"])] != prior["history"] or any(
                row[k] != prior[k]
                for k in ("proof", "source_public", "scope", "policy", "outcome_id", "mandate_sha")
            ):
                raise ValueError("CHECKPOINT_OPPORTUNITY_REWRITE")
            if prior["state"]["phase"] in TERMINAL and row != prior:
                raise ValueError("CHECKPOINT_TERMINAL_REWRITE")
        if before.phase == Phase.COMMITTED_ONCE:
            decision = current.get("checkpoint_decisions", {}).get(row["commitment"])
            if (
                decision is None
                or identity not in decision["opportunity_ids"]
                or decision["result"]["status"] != "COMMITTED_ONCE"
            ):
                raise ValueError("CHECKPOINT_COMMITMENT_MISSING")
        elif row["commitment"] is not None:
            raise ValueError("CHECKPOINT_PREMATURE_COMMITMENT")
    old_decisions = previous.get("checkpoint_decisions", {})
    decisions = current.get("checkpoint_decisions", {})
    if not set(old_decisions).issubset(decisions):
        raise ValueError("CHECKPOINT_DECISION_REMOVED")
    for identity, record in decisions.items():
        expected_id = digest(
            [
                "checkpoint_batch_v3",
                current["account_binding"],
                record["release_sha"],
                sorted(record["opportunity_ids"]),
            ]
        )
        if {
            source_proof(
                rows[k]["proof"],
                rows[k]["source_public"],
                rows[k]["outcome_id"],
                rows[k]["scope"],
                rows[k]["policy"],
            )[0]["checkpoint_ms"]
            for k in record["opportunity_ids"]
        } != {record["result"]["checkpoint_ms"]}:
            raise ValueError("CHECKPOINT_BATCH_CLOCK_BINDING")
        if identity != expected_id or record["result"]["deadline_ms"] != min(
            rows[k]["state"]["deadline_ms"] for k in record["opportunity_ids"]
        ):
            raise ValueError("CHECKPOINT_DECISION_IDENTITY_OR_DEADLINE")
        if identity in old_decisions:
            prior = old_decisions[identity]
            if record["attempts"][: len(prior["attempts"])] != prior["attempts"] or any(
                record[k] != prior[k] for k in ("requests", "opportunity_ids", "release_sha")
            ):
                raise ValueError("CHECKPOINT_DECISION_REWRITE")
        if len(record["attempts"]) > len(old_decisions.get(identity, {}).get("attempts", [])):
            if kind != "CHECKPOINT_REVIEW" or record["attempts"][-1][
                "account_sha"
            ] != account_anchor(previous):
                raise ValueError("CHECKPOINT_DECISION_ACCOUNT_PREDECESSOR")
            _verify_attempt(previous, current, record)
    if kind == "CHECKPOINT_REVIEW" and any(
        previous[k] != current[k]
        for k in (
            "finance",
            "positions",
            "pod_positions",
            "intents",
            "fills",
            "reservations",
            "halts",
            "runtime",
        )
    ):
        raise ValueError("CHECKPOINT_REVIEW_FINANCIAL_EFFECT")
    immutable = (
        "account_binding",
        "endpoint",
        "decision_id",
        "release_sha",
        "market",
        "quantity",
        "limit_price",
        "reduce_only",
        "pod_changes",
        "proof_sha",
        "reservation_id",
        "checkpoint_policy",
        "opportunity_ids",
        "expires_at_ms",
        "created_at_ms",
        "boot_epoch",
        "broker_epoch",
        "account_max_age_ms",
    )
    for identity, intent in current.get("intents", {}).items():
        if intent.get("checkpoint_policy") != POLICY:
            continue
        prior = previous.get("intents", {}).get(identity)
        if prior and any(prior.get(k) != intent.get(k) for k in immutable):
            raise ValueError("CHECKPOINT_INTENT_IMMUTABLE")
        if prior is None and kind != "CHECKPOINT_RESERVE":
            raise ValueError("CHECKPOINT_INTENT_WITHOUT_COMMIT")
        if prior is None:
            _verify_intent(previous, current, intent)
        record = decisions.get(intent["decision_id"])
        if (
            record is None
            or identity not in record["intent_ids"]
            or record["result"]["status"] != "COMMITTED_ONCE"
        ):
            raise ValueError("CHECKPOINT_INTENT_COMMITMENT_BINDING")


def _verify_attempt(
    previous: dict[str, Any], current: dict[str, Any], record: dict[str, Any]
) -> None:
    """Journal independently derives requested notionals and risk from retained proof."""
    attempt = record["attempts"][-1]
    if attempt["risk"] is None:
        return
    policy, markets = attempt["policy"], attempt["markets"]
    mapping = {spec["source_instrument"]: market for market, spec in markets.items()}
    pods, owners = [], set()
    for identity in record["opportunity_ids"]:
        opportunity = current["checkpoint_opportunities"][identity]
        owner = opportunity["state"]["owner"]
        owners.add(owner)
        m = CopyMandate.parse(current["copy_mandates"][owner]["mandate"])
        if digest({"policy": policy, "markets": markets}) != m.data["risk_policy_sha"]:
            raise ValueError("CHECKPOINT_RISK_POLICY_PROOF")
        outcome, marks = source_proof(
            opportunity["proof"],
            opportunity["source_public"],
            opportunity["outcome_id"],
            opportunity["scope"],
            opportunity["policy"],
        )
        expected = {}
        for leg in outcome["positions"]:
            source = leg["instrument"]
            if source not in m.data["allowed_markets"]:
                raise ValueError("CHECKPOINT_REQUEST_MARKET_PROOF")
            expected[mapping[source]] = str(
                decimal(leg["quantity"]) * decimal(marks[source]) * decimal(m.data["k"])
            )
        pods.append({"pod_id": owner, "requested_notionals": expected})
    frozen = {
        owner: dict(book)
        for owner, book in previous["pod_positions"].items()
        if owner not in owners
    }
    for owner in frozen:
        pods.append({"pod_id": owner, "requested_notionals": {}})
    pods.sort(key=lambda pod: pod["pod_id"])
    if pods != attempt["pods"] or frozen != attempt["frozen_pods"]:
        raise ValueError("CHECKPOINT_REQUEST_OR_RESIDUAL_PROOF")
    finance = dict(previous["finance"], pod_loss_used=previous.get("pod_loss_used", {}))
    computed = size_book(
        pods,
        attempt["quotes"],
        markets,
        policy,
        finance,
        previous["positions"],
        previous["pod_positions"],
        [],
        now_ms=attempt["at_ms"],
        quantity_price_policy="EXECUTABLE_SIDE_V3",
        frozen_pods=frozen,
    )
    if computed != attempt["risk"]:
        raise ValueError("CHECKPOINT_RISK_REPLAY")


def _verify_intent(
    previous: dict[str, Any], current: dict[str, Any], intent: dict[str, Any]
) -> None:
    record = current["checkpoint_decisions"][intent["decision_id"]]
    attempt = record["attempts"][-1]
    risk, market = attempt["risk"], intent["market"]
    old = decimal(previous["positions"].get(market, "0"))
    target = decimal(risk["targets"].get(market, "0"))
    expected = target - old
    changes = {
        owner: decimal(book.get(market, "0"))
        - decimal(previous["pod_positions"].get(owner, {}).get(market, "0"))
        for owner, book in risk["pod_targets"].items()
    }
    quote = attempt["quotes"][market]
    slip = decimal(attempt["policy"]["shortfall_bps"]) / 10000
    price = (
        decimal(quote["ask"]) * (1 + slip) if expected > 0 else decimal(quote["bid"]) * (1 - slip)
    )
    tick = decimal(attempt["markets"][market]["price_tick"])
    price = (price / tick).to_integral_value(
        rounding=ROUND_FLOOR if expected > 0 else ROUND_CEILING
    ) * tick
    expected_id = digest(
        {
            "account": current["account_binding"],
            "decision": intent["decision_id"],
            "market": market,
            "quantity": str(expected),
        }
    )
    if (
        intent["intent_id"] != expected_id
        or decimal(intent["quantity"]) != expected
        or {k: decimal(v) for k, v in intent["pod_changes"].items()} != changes
        or sum(changes.values(), ZERO) != expected
        or decimal(intent["limit_price"]) != price
        or intent["reduce_only"]
        is not bool(old and old * expected < 0 and abs(expected) <= abs(old))
        or intent["expires_at_ms"] != record["result"]["deadline_ms"]
        or not attempt["at_ms"] <= intent["created_at_ms"] < intent["expires_at_ms"]
        or intent["proof_sha"] != digest(attempt)
        or intent["release_sha"] != record["release_sha"]
        or intent["reservation_id"] != intent["decision_id"]
        or intent["opportunity_ids"] != record["opportunity_ids"]
        or intent["boot_epoch"] != previous["runtime"]["boot_epoch"]
        or intent["broker_epoch"] != previous["runtime"]["broker_epoch"]
        or intent["account_max_age_ms"]
        != attempt["policy"]["checkpoint_execution"]["account_max_age_ms"]
        or intent["endpoint"] != "fake://broker"
        or current["account_binding"] != "fixture-account"
    ):
        raise ValueError("CHECKPOINT_INTENT_DERIVATION")
    reservation = current["reservations"][intent["decision_id"]]
    if any(
        decimal(reservation[k]) != decimal(risk[r])
        for k, r in (
            ("loss", "worst_loss"),
            ("margin", "stressed_margin"),
            ("cost", "cost_reserve"),
        )
    ):
        raise ValueError("CHECKPOINT_RESERVATION_DERIVATION")
    if (
        account_anchor(previous) != attempt["account_sha"]
        or previous["halts"]
        or previous["reservations"]
    ):
        raise ValueError("CHECKPOINT_RESERVATION_ACCOUNT_CHANGED")
