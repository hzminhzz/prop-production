"""One immutable market/checkpoint claim per existing Guardian account."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .evidence import decimal, digest


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
