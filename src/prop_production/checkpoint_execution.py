"""Project a verified target onto native-safe execution without target chasing."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any, Literal

from .checkpoint_accounting import add
from .checkpoint_source import fields, integer
from .evidence import decimal, digest, require_sha
from .gateway import execution_capability


class OpportunityPhase(StrEnum):
    UNDECIDED = "UNDECIDED"
    REVIEWABLE = "REVIEWABLE"
    PRE_DISPATCH_BLOCKED = "PRE_DISPATCH_BLOCKED"
    COMMITTED_ONCE = "COMMITTED_ONCE"
    MISSED = "MISSED"


class OpportunityEvent(StrEnum):
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"
    COMMIT = "COMMIT"
    MISS = "MISS"


TERMINAL_OPPORTUNITY_PHASES = frozenset({OpportunityPhase.COMMITTED_ONCE, OpportunityPhase.MISSED})


@dataclass(frozen=True)
class OpportunityState:
    opportunity_id: str
    owner: str
    signal_id: str
    deadline_ms: int
    phase: OpportunityPhase = OpportunityPhase.UNDECIDED
    sequence: int = 0
    last_at_ms: int = 0

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self) | {"phase": self.phase.value}

    @classmethod
    def parse(cls, value: dict[str, Any]) -> OpportunityState:
        fields(
            value,
            {
                "opportunity_id",
                "owner",
                "signal_id",
                "deadline_ms",
                "phase",
                "sequence",
                "last_at_ms",
            },
        )
        for key in ("opportunity_id", "owner", "signal_id"):
            require_sha(value[key])
        return cls(
            value["opportunity_id"],
            value["owner"],
            value["signal_id"],
            integer(value["deadline_ms"]),
            OpportunityPhase(value["phase"]),
            integer(value["sequence"]),
            integer(value["last_at_ms"]),
        )


@dataclass(frozen=True)
class OpportunityTransition:
    predecessor: OpportunityState
    event: OpportunityEvent
    evidence_sha: str
    at_ms: int
    actor: str
    guard: str
    successor: OpportunityState
    effect: str

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self) | {
            "event": self.event.value,
            "predecessor": self.predecessor.data,
            "successor": self.successor.data,
        }


def advance(
    before: OpportunityState,
    *,
    event: OpportunityEvent,
    evidence_sha: str,
    at_ms: int,
    guard: str,
    no_side_effect: bool = False,
) -> OpportunityTransition:
    require_sha(evidence_sha)
    integer(at_ms)
    if not isinstance(guard, str) or not guard:
        raise ValueError("OPPORTUNITY_GUARD")
    if before.phase in TERMINAL_OPPORTUNITY_PHASES:
        raise ValueError("OPPORTUNITY_TERMINAL")
    if at_ms < before.last_at_ms:
        raise ValueError("OPPORTUNITY_CLOCK_REGRESSION")
    if event != OpportunityEvent.MISS and at_ms >= before.deadline_ms:
        raise ValueError("OPPORTUNITY_EXPIRED")
    if event == OpportunityEvent.REVIEW:
        if (
            before.phase not in (OpportunityPhase.UNDECIDED, OpportunityPhase.PRE_DISPATCH_BLOCKED)
            or not no_side_effect
        ):
            raise ValueError("OPPORTUNITY_REVIEW_REQUIRES_NO_SIDE_EFFECT")
        phase, effect = OpportunityPhase.REVIEWABLE, "REVIEW_ONLY"
    elif event == OpportunityEvent.BLOCK:
        if before.phase != OpportunityPhase.REVIEWABLE or not no_side_effect:
            raise ValueError("OPPORTUNITY_BLOCK_REQUIRES_NO_SIDE_EFFECT")
        phase, effect = OpportunityPhase.PRE_DISPATCH_BLOCKED, "NO_SIDE_EFFECT"
    elif event == OpportunityEvent.COMMIT:
        if before.phase != OpportunityPhase.REVIEWABLE:
            raise ValueError("OPPORTUNITY_COMMIT_PREDECESSOR")
        phase, effect = OpportunityPhase.COMMITTED_ONCE, "COMMITTED_ONCE"
    else:
        phase, effect = OpportunityPhase.MISSED, "MISSED"
    after = replace(before, phase=phase, sequence=before.sequence + 1, last_at_ms=at_ms)
    return OpportunityTransition(
        before, event, evidence_sha, at_ms, "GUARDIAN", guard, after, effect
    )


@dataclass(frozen=True)
class MarketClaimState:
    resource_id: str
    phase: Literal["UNCLAIMED", "COMMITTED"]
    decision_id: str | None


@dataclass(frozen=True)
class MarketClaimTransition:
    predecessor: MarketClaimState
    event: Literal["COMMIT_BATCH"]
    evidence_sha: str
    at_ms: int
    actor: Literal["GUARDIAN"]
    guard: Literal["ONE_ADJUSTMENT_PER_MARKET_CHECKPOINT"]
    successor: MarketClaimState
    effect: dict[str, Any]

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self)


def affected_markets(state: dict[str, Any], risk: dict[str, Any]) -> list[str]:
    return sorted(
        {
            market
            for owner, book in risk["pod_targets"].items()
            for market in set(book) | set(state["pod_positions"].get(owner, {}))
            if decimal(book.get(market, "0"))
            != decimal(state["pod_positions"].get(owner, {}).get(market, "0"))
        }
    )


def resource(account: str, market: str, checkpoint_ms: int) -> str:
    return digest(["checkpoint_market_v3", account, market, checkpoint_ms])


def conflicts(state: dict[str, Any], risk: dict[str, Any], checkpoint_ms: int) -> bool:
    return any(
        resource(state["account_binding"], market, checkpoint_ms)
        in state.get("checkpoint_market_claims", {})
        for market in affected_markets(state, risk)
    )


def claims(
    state: dict[str, Any], record: dict[str, Any], decision_id: str, now_ms: int
) -> dict[str, Any]:
    checkpoint_ms = record["result"]["checkpoint_ms"]
    attempt = record["attempts"][-1]
    result = {}
    for market in affected_markets(state, attempt["execution_plan"]):
        identity = resource(state["account_binding"], market, checkpoint_ms)
        if identity in state.get("checkpoint_market_claims", {}):
            raise ValueError("MARKET_CHECKPOINT_ALREADY_COMMITTED")
        before = MarketClaimState(identity, "UNCLAIMED", None)
        after = MarketClaimState(identity, "COMMITTED", decision_id)
        result[identity] = MarketClaimTransition(
            before,
            "COMMIT_BATCH",
            digest(attempt),
            now_ms,
            "GUARDIAN",
            "ONE_ADJUSTMENT_PER_MARKET_CHECKPOINT",
            after,
            {
                "market": market,
                "checkpoint_ms": checkpoint_ms,
                "account": state["account_binding"],
                "source_signal_ids": record["result"]["source_signal_ids"],
            },
        ).data
    return result


def validate_claims(previous: dict[str, Any], current: dict[str, Any], kind: str) -> None:
    old, new = (
        previous.get("checkpoint_market_claims", {}),
        current.get("checkpoint_market_claims", {}),
    )
    if not set(old) <= set(new) or any(new[k] != row for k, row in old.items()):
        raise ValueError("CHECKPOINT_MARKET_CLAIM_REWRITE")
    added = set(new) - set(old)
    if not added:
        if kind == "CHECKPOINT_RESERVE":
            committed = [
                key
                for key, record in current.get("checkpoint_decisions", {}).items()
                if record["result"]["status"] == "COMMITTED_ONCE"
                and previous.get("checkpoint_decisions", {})
                .get(key, {})
                .get("result", {})
                .get("status")
                != "COMMITTED_ONCE"
            ]
            if any(
                affected_markets(
                    previous, current["checkpoint_decisions"][key]["attempts"][-1]["execution_plan"]
                )
                for key in committed
            ):
                raise ValueError("CHECKPOINT_MARKET_CLAIM_MISSING")
        return
    if kind != "CHECKPOINT_RESERVE":
        raise ValueError("CHECKPOINT_MARKET_CLAIM_WITHOUT_COMMIT")
    decision_ids = {new[k]["successor"]["decision_id"] for k in added}
    if len(decision_ids) != 1:
        raise ValueError("CHECKPOINT_MARKET_CLAIM_BATCH")
    identity = next(iter(decision_ids))
    record = current["checkpoint_decisions"][identity]
    now_ms = new[next(iter(added))]["at_ms"]
    expected = claims(previous, record, identity, now_ms)
    if {k: new[k] for k in added} != expected or record["result"]["status"] != "COMMITTED_ONCE":
        raise ValueError("CHECKPOINT_MARKET_CLAIM_REPLAY")


@dataclass(frozen=True)
class ExecutionProjection:
    schema: Literal["checkpoint_execution_projection_v1", "checkpoint_execution_projection_v2"]
    mode: Literal["NET", "REDUCE_ONLY_FALLBACK"]
    opening_status: Literal["ELIGIBLE", "MISSED"]
    targets: dict[str, str]
    pod_targets: dict[str, dict[str, str]]
    reduce_only: dict[str, bool]
    missed_owners: tuple[str, ...] = ()

    @property
    def data(self) -> dict[str, Any]:
        result = asdict(self)
        if self.schema == "checkpoint_execution_projection_v1":
            # Historical journal contents must replay byte-for-byte.
            result.pop("missed_owners")
        else:
            result["missed_owners"] = list(self.missed_owners)
        return result


def opening_missed(plan: dict[str, Any], owner: str) -> bool:
    """Interpret one opportunity against the projection generation that created it."""
    if plan["schema"] == "checkpoint_execution_projection_v1":
        return bool(plan["opening_status"] == "MISSED")
    if plan["schema"] == "checkpoint_execution_projection_v2":
        missed = plan["missed_owners"]
        if (
            not isinstance(missed, list)
            or missed != sorted(set(missed))
            or (plan["opening_status"] == "MISSED") != bool(missed)
        ):
            raise ValueError("CHECKPOINT_OPENING_DISPOSITION_PROOF")
        return owner in missed
    raise ValueError("CHECKPOINT_EXECUTION_PROJECTION_VERSION")


def project(
    state: dict[str, Any],
    risk: dict[str, Any],
    proof: dict[str, Any],
    markets: dict[str, Any],
    quotes: dict[str, Any],
    frozen: dict[str, dict[str, str]],
    *,
    now_ms: int,
    synthetic: bool,
    version: Literal[
        "checkpoint_execution_projection_v1", "checkpoint_execution_projection_v2"
    ] = "checkpoint_execution_projection_v2",
) -> dict[str, Any]:
    def permits(market: str, feature: str) -> bool:
        raw = proof.get("execution_capabilities", {}).get(market)
        return raw is not None and execution_capability(
            raw,
            feature,
            account=state["account_binding"],
            endpoint=proof["endpoint"],
            instrument=market,
            now_ms=now_ms,
            synthetic=synthetic,
        )

    if version not in {
        "checkpoint_execution_projection_v1",
        "checkpoint_execution_projection_v2",
    }:
        raise ValueError("CHECKPOINT_EXECUTION_PROJECTION_VERSION")
    books = deepcopy(risk["pod_targets"])
    all_markets = sorted(set(risk["targets"]) | set(state["positions"]))
    fallback_markets: set[str] = set()
    for market in all_markets:
        old = decimal(state["positions"].get(market, "0"))
        target = decimal(risk["targets"].get(market, "0"))
        if old * target < 0 and not permits(market, "cross_zero"):
            fallback_markets.add(market)
        for owner, residual in frozen.items():
            held = decimal(residual.get(market, "0"))
            if any(
                held * decimal(book.get(market, "0")) < 0
                for pod, book in books.items()
                if pod != owner and pod not in frozen
            ):
                fallback_markets.add(market)

    # Preserve the exact v1 projection for immutable historical financial replay.
    # Only v2 applies a failure to its actual instrument instead of every market.
    fenced_markets = (
        set(all_markets)
        if fallback_markets and version == "checkpoint_execution_projection_v1"
        else fallback_markets
    )
    for owner, book in books.items():
        if owner in frozen:
            continue
        current = state["pod_positions"].get(owner, {})
        for market in sorted(fenced_markets & (set(book) | set(current))):
            old = decimal(current.get(market, "0"))
            desired = decimal(book.get(market, "0"))
            book[market] = str(
                min(abs(old), abs(desired)) * (1 if old > 0 else -1)
                if old * desired > 0
                else decimal("0")
            )
    missed_owners = tuple(
        sorted(
            owner
            for owner, original in risk["pod_targets"].items()
            if owner not in frozen
            and any(
                decimal(original.get(market, "0")) != decimal(books[owner].get(market, "0"))
                for market in fallback_markets
            )
        )
    )
    targets = {
        market: str(add(*(decimal(book.get(market, "0")) for book in books.values())))
        for market in all_markets
    }
    reducing = {}
    for market in all_markets:
        old, target = decimal(state["positions"].get(market, "0")), decimal(targets[market])
        quantity = add(target, -old)
        reduce_only = bool(old and old * quantity < 0 and abs(quantity) <= abs(old))
        reducing[market] = reduce_only
        if not quantity:
            continue
        if market in fenced_markets and not reduce_only:
            raise ValueError("NO_PROVABLE_ACCOUNT_REDUCTION")
        if reduce_only and not permits(market, "reduce_only"):
            raise ValueError("REDUCE_ONLY_CAPABILITY_UNQUALIFIED")
        if old:
            basis = state.get("checkpoint_basis", {}).get(market)
            observed = proof.get("position_basis", {}).get(market)
            if (
                basis is None
                or observed != basis
                or decimal(basis["quantity"]) != old
                or decimal(basis["entry_price"]) <= 0
            ):
                raise ValueError("INDEPENDENT_ENTRY_BASIS_UNQUALIFIED")
        spec = markets[market]
        step = decimal(spec["qty_step"])
        executable = decimal(quotes[market]["ask"] if quantity > 0 else quotes[market]["bid"])
        if (
            quantity % step
            or abs(quantity) < decimal(spec["min_qty"])
            or abs(quantity * executable) < decimal(spec["min_notional"])
        ):
            raise ValueError("NATIVE_ADJUSTMENT_LOT")
    return ExecutionProjection(
        version,
        "REDUCE_ONLY_FALLBACK" if fallback_markets else "NET",
        "MISSED"
        if (fallback_markets if version == "checkpoint_execution_projection_v1" else missed_owners)
        else "ELIGIBLE",
        targets,
        books,
        reducing,
        missed_owners,
    ).data
