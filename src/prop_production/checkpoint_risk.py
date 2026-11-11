"""Risk obligations follow their immutable financial owner across release generations."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from .evidence import decimal, digest


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
