"""Translate already verified Guardian evidence into immutable kernel inputs."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from copytrade_kernel import (
    AccountState,
    CheckpointBatch,
    CheckpointMs,
    DecisionInputs,
    DecisionMs,
    FixedKSizing,
    FrozenObject,
    KernelState,
    KnownAtTs,
    MarketContext,
    NativeInstrument,
    OpeningMissed,
    OwnerBook,
    ProspectiveCWSA,
    RiskPolicy,
    SourceLeg,
    SourceOutcome,
    VenueCapability,
)

from .checkpoint_risk import effective_policy
from .evidence import decimal, digest
from .gateway import execution_capability


@dataclass(frozen=True, slots=True)
class VerifiedChange:
    outcome: dict[str, Any]
    marks: dict[str, str]
    source: ProspectiveCWSA
    k: str
    deadline_ms: int


def kernel_state(
    state: dict[str, Any],
    owners: set[str],
    mapping: dict[str, str],
    checkpoint: CheckpointMs,
) -> KernelState:
    """Rebuild entry memory from previously verified terminal opportunities."""
    history: list[tuple[int, str, str, dict[str, Any], str | None]] = []
    commitments = {
        opportunity: (decision_id, decision, attempt["execution_plan"])
        for decision_id, decision in state.get("checkpoint_decisions", {}).items()
        if decision["result"]["status"] == "COMMITTED_ONCE"
        for attempt in decision["attempts"][-1:]
        for opportunity in decision["opportunity_ids"]
    }
    for identity, record in state.get("checkpoint_opportunities", {}).items():
        row = record["state"]
        owner, phase = row["owner"], row["phase"]
        if owner not in owners or phase not in {"MISSED", "COMMITTED_ONCE"}:
            continue
        outcome = next(
            event["effect"]
            for event in record["proof"]["payload"]["transitions"]
            if event["effect"] is not None and event["effect"]["outcome_id"] == record["outcome_id"]
        )
        if outcome["checkpoint_ms"] < checkpoint.value:
            history.append((outcome["checkpoint_ms"], identity, owner, outcome, phase))
    books: dict[str, dict[NativeInstrument, OpeningMissed]] = {owner: {} for owner in owners}
    zero = Decimal(0)
    for at, identity, owner, outcome, phase in sorted(history, key=lambda row: row[:2]):
        memory = books[owner]
        commitment = commitments.get(identity)
        targets = {} if commitment is None else commitment[2]["pod_targets"].get(owner, {})
        for leg in outcome["positions"]:
            instrument = NativeInstrument(leg["instrument"])
            if instrument.key not in mapping:
                continue
            quantity = decimal(leg["quantity"])
            market = mapping[instrument.key]
            target = decimal(targets.get(market, "0"))
            was_opening = bool(target and (quantity > zero) == (target > zero))
            if (
                was_opening
                and commitment is not None
                and _attributed(
                    state,
                    commitment[0],
                    commitment[1],
                    owner,
                    market,
                    quantity,
                )
            ):
                memory.pop(instrument, None)
            elif phase == "MISSED" or was_opening:
                previous = memory.get(instrument)
                if previous is None or previous.allows_retry(quantity):
                    memory[instrument] = OpeningMissed(CheckpointMs(at), quantity)
    return KernelState(
        tuple(
            OwnerBook(
                owner,
                tuple(
                    sorted(
                        (market, decimal(quantity))
                        for market, quantity in state["pod_positions"].get(owner, {}).items()
                    )
                ),
                tuple(sorted(books[owner].items())),
            )
            for owner in sorted(owners)
        )
    )


def _attributed(
    state: dict[str, Any],
    decision_id: str,
    decision: dict[str, Any],
    owner: str,
    market: str,
    quantity: Decimal,
) -> bool:
    """An intent alone is not a completed entry. Use verified owner effects."""
    internal = state.get("checkpoint_internal_effects", {}).get(digest([decision_id, market]))
    if (
        internal is not None
        and decimal(internal["effects"]["quantities"].get(owner, "0")) * quantity > 0
    ):
        return True
    ids = set(decision["intent_ids"])
    return any(
        fill["intent_id"] in ids
        and fill["market"] == market
        and decimal(
            state.get("checkpoint_allocations", {})
            .get(fill_id, {})
            .get("effects", {})
            .get(owner, {})
            .get("quantity", "0")
        )
        * quantity
        > 0
        for fill_id, fill in state["fills"].items()
    )


def provenance(record: dict[str, Any]) -> ProspectiveCWSA:
    packet = record["proof"]
    event = next(
        row
        for row in packet["payload"]["transitions"]
        if row["effect"] is not None and row["effect"]["outcome_id"] == record["outcome_id"]
    )
    return ProspectiveCWSA(event["sequence"], digest(event), digest(packet))


def checkpoint_batch(
    changes: dict[str, VerifiedChange],
    mapping: dict[str, str],
    checkpoint: CheckpointMs,
) -> CheckpointBatch[ProspectiveCWSA]:
    return CheckpointBatch(
        checkpoint,
        tuple(
            SourceOutcome(
                owner,
                FixedKSizing(decimal(change.k)),
                KnownAtTs(change.outcome["published_ms"]),
                DecisionMs(change.deadline_ms),
                tuple(
                    SourceLeg(
                        NativeInstrument(leg["instrument"]),
                        mapping[leg["instrument"]],
                        decimal(leg["quantity"]),
                        decimal(change.marks[leg["instrument"]]),
                    )
                    for leg in change.outcome["positions"]
                ),
                change.source,
            )
            for owner, change in sorted(changes.items())
        ),
    )


def decision_inputs(
    state: dict[str, Any],
    policy: dict[str, Any],
    markets: dict[str, Any],
    quotes: dict[str, Any],
    proof: dict[str, Any],
    now_ms: int,
    *,
    synthetic: bool,
) -> DecisionInputs:
    def permitted(symbol: str, evidence: dict[str, Any], feature: str) -> bool:
        return execution_capability(
            evidence,
            feature,
            account=state["account_binding"],
            endpoint=proof["endpoint"],
            instrument=symbol,
            now_ms=now_ms,
            synthetic=synthetic,
        )

    capabilities = tuple(
        VenueCapability(
            symbol,
            permitted(symbol, evidence, "cross_zero"),
            permitted(symbol, evidence, "reduce_only"),
        )
        for symbol, evidence in sorted(proof.get("execution_capabilities", {}).items())
    )
    return DecisionInputs(
        AccountState(
            FrozenObject.parse(
                dict(state["finance"], pod_loss_used=state.get("pod_loss_used", {}))
            ),
            FrozenObject.parse(state["positions"]),
            FrozenObject.parse(state["pod_positions"]),
            FrozenObject.parse(state.get("checkpoint_basis", {})),
            FrozenObject.parse(proof.get("position_basis", {})),
        ),
        MarketContext(FrozenObject.parse(quotes), FrozenObject.parse(markets), capabilities),
        RiskPolicy(FrozenObject.parse(effective_policy(state, policy, markets))),
        DecisionMs(now_ms),
    )
