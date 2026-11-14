"""Declared stress-envelope sizing of the complete book with native lot constraints."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Any

from .evidence import decimal
from .planner import validate_quotes

ZERO = Decimal("0")
ONE = Decimal("1")


def fixture_policy() -> dict[str, Any]:
    return {
        "schema": "risk_policy_v1",
        "source_mode": "SYNTHETIC",
        "scale_steps": 1000,
        "minimum_headroom": "10",
        "made_limit": "400",
        "pod_loss_budget": "200",
        "hedge_tolerance": "0.05",
        "shortfall_bps": "30",
        "margin_multiplier": "5",
        "scenarios": [
            {"BTC": "-0.1", "ETH": "-0.15"},
            {"BTC": "0.1", "ETH": "0.15"},
            {"BTC": "-0.1", "ETH": "0.15"},
            {"BTC": "0.1", "ETH": "-0.15"},
        ],
    }


def fixture_markets() -> dict[str, Any]:
    def spec() -> dict[str, Any]:
        return {
            "source_mode": "SYNTHETIC",
            "verified": True,
            "valid_until_ms": 10**15,
            "qty_step": "0.001",
            "price_tick": "0.01",
            "min_qty": "0.001",
            "min_notional": "5",
            "liquidity_notional": "100000",
            "initial_rate": "0.1",
            "fee_rate": "0.001",
            "funding_bound": "0.001",
            "liquidation_rate": "0.005",
            "tiers": [{"cap": "1000000", "rate": "0.01"}],
        }

    return {"BTC": spec(), "ETH": spec()}


def _bounds(
    current: dict[str, Decimal], target: dict[str, Decimal], pending: list[dict[str, Any]]
) -> dict[str, tuple[Decimal, Decimal]]:
    result = {
        m: (
            min(current.get(m, ZERO), target.get(m, ZERO)),
            max(current.get(m, ZERO), target.get(m, ZERO)),
        )
        for m in current.keys() | target.keys()
    }
    for order in pending:
        m, quantity = order["market"], decimal(order["remaining"])
        lo, hi = result.get(m, (ZERO, ZERO))
        result[m] = (lo + min(quantity, ZERO), hi + max(quantity, ZERO))
    return result


def _loss(
    bounds: dict[str, tuple[Decimal, Decimal]],
    prices: dict[str, Decimal],
    scenarios: list[dict[str, Any]],
) -> Decimal:
    # Each leg can fill independently: no unearned credit for simultaneous hedge execution.
    return max(
        [ZERO]
        + [
            -sum(
                (
                    min(
                        lo * prices[m] * decimal(scenario[m]), hi * prices[m] * decimal(scenario[m])
                    )
                    for m, (lo, hi) in bounds.items()
                ),
                ZERO,
            )
            for scenario in scenarios
        ]
    )


def validate_market_inputs(
    markets: dict[str, Any], policy: dict[str, Any], needed: set[str], now_ms: int
) -> None:
    if (
        policy.get("schema") != "risk_policy_v1"
        or type(policy.get("scale_steps")) is not int
        or not 1 <= policy["scale_steps"] <= 1000
        or not policy.get("scenarios")
    ):
        raise ValueError("RISK_POLICY_UNKNOWN")
    for field in (
        "minimum_headroom",
        "made_limit",
        "pod_loss_budget",
        "hedge_tolerance",
        "shortfall_bps",
        "margin_multiplier",
    ):
        if decimal(policy[field]) <= 0:
            raise ValueError("RISK_POLICY_AMOUNTS")
    if decimal(policy["shortfall_bps"]) < 30:
        raise ValueError("SHORTFALL_BELOW_30_BPS")
    if decimal(policy["hedge_tolerance"]) > Decimal("0.1"):
        raise ValueError("HEDGE_TOLERANCE")
    allowed_instruments: set[str] | None = None
    if "allowed_instrument_types" in policy:
        raw_instruments = policy["allowed_instrument_types"]
        if not isinstance(raw_instruments, (list, tuple)) or not raw_instruments or not all(
            isinstance(item, str) and item.strip() for item in raw_instruments
        ):
            raise ValueError("RISK_POLICY_UNKNOWN")
        allowed_instruments = {item.strip() for item in raw_instruments}
    blocked_symbols: set[str] = set()
    if "blocked_symbols" in policy:
        raw_blocked = policy["blocked_symbols"]
        if not isinstance(raw_blocked, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in raw_blocked
        ):
            raise ValueError("RISK_POLICY_UNKNOWN")
        blocked_symbols = {item.strip().upper() for item in raw_blocked}
    if ("low_cap_mcap_threshold_usd" in policy) != ("low_cap_max_notional" in policy):
        raise ValueError("RISK_POLICY_UNKNOWN")
    if "low_cap_mcap_threshold_usd" in policy:
        if (
            decimal(policy["low_cap_mcap_threshold_usd"]) <= 0
            or decimal(policy["low_cap_max_notional"]) <= 0
        ):
            raise ValueError("RISK_POLICY_AMOUNTS")
    for m in needed:
        spec = markets[m]
        if m.strip().upper() in blocked_symbols or str(spec.get("symbol", "")).strip().upper() in blocked_symbols:
            raise ValueError("CHALLENGE_SYMBOL_PROHIBITED")
        if allowed_instruments is not None and spec.get("instrument_type") not in allowed_instruments:
            raise ValueError("CHALLENGE_INSTRUMENT_PROHIBITED")
        if (
            spec["verified"] is not True
            or spec["source_mode"] != policy["source_mode"]
            or type(spec["valid_until_ms"]) is not int
            or spec["valid_until_ms"] <= now_ms
        ):
            raise ValueError("NATIVE_MARKET_UNVERIFIED")
        for field in (
            "qty_step",
            "price_tick",
            "min_qty",
            "min_notional",
            "liquidity_notional",
            "initial_rate",
        ):
            if decimal(spec[field]) <= 0:
                raise ValueError("NATIVE_MARKET_INVALID")
        for field in ("fee_rate", "funding_bound", "liquidation_rate"):
            if decimal(spec[field]) < 0:
                raise ValueError("COST_EVIDENCE_INVALID")
        previous_cap = ZERO
        if not spec["tiers"]:
            raise ValueError("MAINTENANCE_TIERS_UNKNOWN")
        for tier in spec["tiers"]:
            cap, rate = decimal(tier["cap"]), decimal(tier["rate"])
            if cap <= previous_cap or rate <= 0:
                raise ValueError("MAINTENANCE_TIERS_INVALID")
            previous_cap = cap
        for scenario in policy["scenarios"]:
            if m not in scenario or not -ONE < decimal(scenario[m]) < ONE:
                raise ValueError("STRESS_MARKET_UNCOVERED")


def size_book(
    pods: list[dict[str, Any]],
    quotes: dict[str, Any],
    markets: dict[str, Any],
    policy: dict[str, Any],
    finance: dict[str, Any],
    positions: dict[str, str],
    pod_positions: dict[str, dict[str, str]],
    pending: list[dict[str, Any]],
    *,
    now_ms: int,
    quantity_price_policy: str = "LEGACY_MID",
    frozen_pods: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    try:
        if quantity_price_policy not in {"LEGACY_MID", "EXECUTABLE_SIDE_V3"}:
            raise ValueError("QUANTITY_PRICE_POLICY")
        validate_quotes(quotes, now_ms)
        needed = (
            set(positions)
            | {o["market"] for o in pending}
            | {m for p in pods for m in p["requested_notionals"]}
            | {m for book in pod_positions.values() for m in book}
        )
        validate_market_inputs(markets, policy, needed, now_ms)
        if not needed.issubset(quotes):
            raise ValueError("QUOTE_MISSING")
        if len({p["pod_id"] for p in pods}) != len(pods) or not pods:
            raise ValueError("POD_IDENTITY")
        if not finance or decimal(finance["headroom"]) <= 0:
            raise ValueError("ACCOUNT_HEADROOM_UNKNOWN")
        with localcontext() as context:
            context.prec = 50
            return _size(
                pods,
                quotes,
                markets,
                policy,
                finance,
                positions,
                pod_positions,
                pending,
                quantity_price_policy=quantity_price_policy,
                frozen_pods=frozen_pods or {},
            )
    except (KeyError, ValueError, TypeError) as exc:
        return {
            "status": "BLOCKED",
            "reasons": [str(exc) if isinstance(exc, ValueError) else "RISK_INPUT_INCOMPLETE"],
        }


def _size(
    pods: list[dict[str, Any]],
    quotes: dict[str, Any],
    markets: dict[str, Any],
    policy: dict[str, Any],
    finance: dict[str, Any],
    positions: dict[str, str],
    pod_positions: dict[str, dict[str, str]],
    pending: list[dict[str, Any]],
    *,
    quantity_price_policy: str,
    frozen_pods: dict[str, dict[str, str]],
) -> dict[str, Any]:
    prices = {m: (decimal(q["bid"]) + decimal(q["ask"])) / 2 for m, q in quotes.items()}
    current = {m: decimal(q) for m, q in positions.items()}
    checkpoint = quantity_price_policy == "EXECUTABLE_SIDE_V3"
    gross_limit = (
        decimal(policy["checkpoint_execution"]["max_gross_notional"]) if checkpoint else None
    )
    if gross_limit is not None and gross_limit <= 0:
        raise ValueError("GROSS_EXPOSURE_POLICY")
    headroom = decimal(finance["headroom"]) - decimal(policy["minimum_headroom"])
    made_remaining = decimal(policy["made_limit"]) - decimal(finance["realized_made"])
    limit = min(headroom, made_remaining)
    reasons: set[str] = set()
    for scale_units in range(policy["scale_steps"], 0, -1):
        scale = Decimal(scale_units) / policy["scale_steps"]
        pod_targets: dict[str, dict[str, Decimal]] = {}
        target: dict[str, Decimal] = {}
        valid = True
        fidelities = []
        for pod in pods:
            book: dict[str, Decimal] = {}
            ratios = []
            if pod["pod_id"] in frozen_pods:
                book = {m: decimal(q) for m, q in frozen_pods[pod["pod_id"]].items()}
                pod_targets[pod["pod_id"]] = book
                for m, quantity in book.items():
                    target[m] = target.get(m, ZERO) + quantity
                continue
            for m, raw in pod["requested_notionals"].items():
                requested = decimal(raw)
                if requested == 0:
                    continue
                spec = markets[m]
                step = decimal(spec["qty_step"])
                quantity_price = (
                    decimal(quotes[m]["ask"] if requested > 0 else quotes[m]["bid"])
                    if quantity_price_policy == "EXECUTABLE_SIDE_V3"
                    else prices[m]
                )
                quantity = (requested * scale / quantity_price / step).to_integral_value(
                    rounding=ROUND_DOWN
                ) * step
                notional = abs(quantity * quantity_price)
                if (
                    quantity == 0
                    or abs(quantity) < decimal(spec["min_qty"])
                    or notional < decimal(spec["min_notional"])
                ):
                    valid = False
                    reasons.add("WHOLE_POD_NATIVE_ROUNDING")
                    break
                ratios.append(abs(quantity * quantity_price / requested) / scale)
                book[m] = quantity
                target[m] = target.get(m, ZERO) + quantity
            if ratios:
                fidelity = min(ratios)
                fidelities.append(fidelity)
                if fidelity < ONE - decimal(policy["hedge_tolerance"]):
                    valid = False
                    reasons.add("HEDGE_FIDELITY")
            pod_targets[pod["pod_id"]] = book
        if not valid:
            continue
        bounds = _bounds(current, target, pending)
        pod_bounds = {
            pod["pod_id"]: _bounds(
                {m: decimal(q) for m, q in pod_positions.get(pod["pod_id"], {}).items()},
                pod_targets[pod["pod_id"]],
                [],
            )
            for pod in pods
        }
        gross_by_market = {
            m: sum(
                (
                    max(abs(b.get(m, (ZERO, ZERO))[0]), abs(b.get(m, (ZERO, ZERO))[1]))
                    for b in pod_bounds.values()
                ),
                ZERO,
            )
            for m in bounds
        }
        for order in pending:
            gross_by_market[order["market"]] = gross_by_market.get(order["market"], ZERO) + abs(
                decimal(order["remaining"])
            )
        gross_notional = sum((q * prices[m] for m, q in gross_by_market.items()), ZERO)
        if gross_limit is not None and gross_notional > gross_limit:
            reasons.add("GROSS_EXPOSURE_CAP")
            continue
        cost = ZERO
        costs_by_market: dict[str, Decimal] = {}
        margin = ZERO
        low_cap_threshold = (
            decimal(policy["low_cap_mcap_threshold_usd"])
            if "low_cap_mcap_threshold_usd" in policy
            else None
        )
        low_cap_max = (
            decimal(policy["low_cap_max_notional"]) if "low_cap_max_notional" in policy else None
        )
        for m, (lo, hi) in bounds.items():
            spec = markets[m]
            max_qty = gross_by_market[m] if checkpoint else max(abs(lo), abs(hi))
            if low_cap_threshold is not None and low_cap_max is not None:
                is_low_cap = spec.get("is_low_cap") is True or (
                    spec.get("market_cap_usd") is not None
                    and decimal(spec["market_cap_usd"]) < low_cap_threshold
                )
                if is_low_cap and max_qty * prices[m] > low_cap_max:
                    valid = False
                    reasons.add("LOW_CAP_EXPOSURE_CAP")
                    break
            max_shock = max(abs(decimal(s[m])) for s in policy["scenarios"])
            stressed_notional = max_qty * prices[m] * (ONE + max_shock)
            tier = next((t for t in spec["tiers"] if stressed_notional <= decimal(t["cap"])), None)
            if tier is None:
                valid = False
                reasons.add("MARGIN_TIER_UNCOVERED")
                break
            if stressed_notional > decimal(spec["liquidity_notional"]):
                valid = False
                reasons.add("LIQUIDITY_CAP")
                break
            # Reserve close-out, funding and liquidation costs for the entire possible exposure.
            spread = (decimal(quotes[m]["ask"]) - decimal(quotes[m]["bid"])) / prices[m]
            delta = (
                sum(
                    (
                        abs(
                            pod_targets[p["pod_id"]].get(m, ZERO)
                            - decimal(pod_positions.get(p["pod_id"], {}).get(m, "0"))
                        )
                        for p in pods
                    ),
                    ZERO,
                )
                if checkpoint
                else abs(target.get(m, ZERO) - current.get(m, ZERO))
            )
            opening_cost = (
                delta
                * prices[m]
                * (spread + decimal(policy["shortfall_bps"]) / 10000 + decimal(spec["fee_rate"]))
            )
            exit_cost = stressed_notional * (
                decimal(spec["fee_rate"])
                + decimal(spec["funding_bound"])
                + decimal(spec["liquidation_rate"])
                + decimal(policy["shortfall_bps"]) / 10000
                + spread
            )
            costs_by_market[m] = opening_cost + exit_cost
            cost += costs_by_market[m]
            margin += stressed_notional * max(
                decimal(spec["initial_rate"]),
                decimal(tier["rate"]) * decimal(policy["margin_multiplier"]),
            )
        if not valid:
            continue
        loss = (
            sum((_loss(b, prices, policy["scenarios"]) for b in pod_bounds.values()), ZERO)
            if checkpoint
            else _loss(bounds, prices, policy["scenarios"])
        ) + cost
        pod_losses = {}
        for pod in pods:
            name = pod["pod_id"]
            pbounds = _bounds(
                {m: decimal(q) for m, q in pod_positions.get(name, {}).items()},
                pod_targets[name],
                [],
            )
            # Attribute total market costs conservatively to every pod using that market.
            pod_cost = sum((costs_by_market.get(m, ZERO) for m in pbounds), ZERO)
            pod_loss = _loss(pbounds, prices, policy["scenarios"]) + pod_cost
            pod_losses[name] = str(pod_loss)
            used = decimal(finance.get("pod_loss_used", {}).get(name, "0"))
            if used < 0:
                raise ValueError("POD_BUDGET_ACCOUNTING_INVALID")
            if pod_loss + used > decimal(policy["pod_loss_budget"]):
                valid = False
                reasons.add("POD_LOSS_BUDGET")
        if loss > limit:
            valid = False
            reasons.add("ACCOUNT_MADE_OR_FLOOR")
        if margin + loss + decimal(policy["minimum_headroom"]) > decimal(finance["equity"]):
            valid = False
            reasons.add("STRESSED_MARGIN")
        if valid:
            return {
                "status": "ACCEPTED",
                "reasons": [],
                "scale": str(scale),
                **({"gross_notional": str(gross_notional)} if checkpoint else {}),
                "targets": {m: str(q) for m, q in sorted(target.items())},
                "pod_targets": {
                    p: {m: str(q) for m, q in b.items()} for p, b in pod_targets.items()
                },
                "worst_loss": str(loss),
                "cost_reserve": str(cost),
                "stressed_margin": str(margin),
                "pod_losses": pod_losses,
                "headroom_after_stress": str(decimal(finance["headroom"]) - loss),
                "fidelity": str(min(fidelities, default=ONE)),
                "execution_shortfall_bps": policy["shortfall_bps"],
                "binding_constraints": sorted(reasons),
                "tracking_error": str(ONE - scale * min(fidelities, default=ONE)),
                "approved_notionals": {m: str(q * prices[m]) for m, q in target.items()},
                "scope": "BOUNDED_STRESS_MODEL",
                "trading_authorized": False,
            }
    return {
        "status": "NO_TRADE",
        "reasons": sorted(reasons or {"NO_FEASIBLE_WHOLE_BOOK"}),
        "scale": "0",
        "trading_authorized": False,
    }
