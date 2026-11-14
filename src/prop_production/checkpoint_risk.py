"""Risk obligations follow their immutable financial owner across release generations."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from .checkpoint_account_proof import capture
from .checkpoint_account_proof import validate as validate_account_proof
from .checkpoint_source import integer
from .evidence import decimal, digest
from .gateway import BrokerGateway, execution_capability
from .planner import validate_quotes


def loss_limit(state: dict[str, Any], owner: str, policy: dict[str, Any]) -> Decimal:
    entry = state.get("copy_mandates", {}).get(owner)
    if entry is None:
        raise ValueError("RETAINED_OWNER_RISK_UNQUALIFIED")
    contract = entry["risk_contract"]
    if digest(contract) != entry["mandate"]["risk_policy_sha"]:
        raise ValueError("RETAINED_OWNER_RISK_PROOF")
    return min(decimal(policy["pod_loss_budget"]), decimal(contract["policy"]["pod_loss_budget"]))


def effective_policy(
    state: dict[str, Any],
    policy: dict[str, Any],
    markets: dict[str, Any],
) -> dict[str, Any]:
    """Intersect approved constraints while any previous owner retains exposure."""
    result = deepcopy(policy)
    scenarios = {digest(s): s for s in policy["scenarios"]}
    for owner, book in state["pod_positions"].items():
        held = {market for market, quantity in book.items() if decimal(quantity)}
        if not held:
            continue
        entry = state.get("copy_mandates", {}).get(owner)
        if entry is None:
            raise ValueError("RETAINED_OWNER_RISK_UNQUALIFIED")
        contract = entry["risk_contract"]
        if digest(contract) != entry["mandate"]["risk_policy_sha"]:
            raise ValueError("RETAINED_OWNER_RISK_PROOF")
        prior = contract["policy"]
        if prior["source_mode"] != policy["source_mode"] or any(
            contract["markets"].get(market) != markets.get(market) for market in held
        ):
            raise ValueError("RETAINED_NATIVE_SPEC_REQUALIFICATION_REQUIRED")
        for field in ("minimum_headroom", "shortfall_bps", "margin_multiplier"):
            result[field] = str(max(decimal(result[field]), decimal(prior[field])))
        for field in ("made_limit", "pod_loss_budget", "hedge_tolerance"):
            result[field] = str(min(decimal(result[field]), decimal(prior[field])))
        for field in ("quote_max_age_ms", "account_max_age_ms"):
            result["checkpoint_execution"][field] = min(
                result["checkpoint_execution"][field],
                prior["checkpoint_execution"][field],
            )
        result["checkpoint_execution"]["max_gross_notional"] = str(
            min(
                decimal(result["checkpoint_execution"]["max_gross_notional"]),
                decimal(prior["checkpoint_execution"]["max_gross_notional"]),
            )
        )
        scenarios.update({digest(s): s for s in prior["scenarios"]})
    # Preserve the original signed ordering when there is no additional scenario.
    seen = {digest(s) for s in result["scenarios"]}
    result["scenarios"].extend(scenarios[k] for k in sorted(set(scenarios) - seen))
    return result


def execution_policy(
    state: dict[str, Any], policy: dict[str, Any], markets: dict[str, Any]
) -> dict[str, Any]:
    cfg = policy.get("checkpoint_execution")
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
    return dict(effective_policy(state, policy, markets)["checkpoint_execution"])


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


def check_prerequisites(
    gateway: BrokerGateway,
    state: dict[str, Any],
    quotes: dict[str, Any],
    cfg: dict[str, Any],
    now_ms: int,
    needed: set[str],
    *,
    capability_feature: str = "net_order",
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
        snapshot = gateway.observe(now_ms)
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
                capability_feature,
                account=state["account_binding"],
                endpoint=snapshot.endpoint,
                instrument=market,
                now_ms=now_ms,
                synthetic=gateway.synthetic,
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
        evidence = capture(snapshot)
        validate_account_proof(
            state,
            evidence,
            quotes,
            cfg,
            needed,
            now_ms=now_ms,
            synthetic=gateway.synthetic,
            feature=capability_feature,
        )
    except (ValueError, KeyError, TimeoutError, ConnectionError, OSError) as exc:
        reasons.append(str(exc))
    return reasons, evidence
