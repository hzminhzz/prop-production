"""Guardian challenge rulebook evidence and adapter policies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from copytrade_kernel.rulebooks import ChallengeRulebookSpec as KernelChallengeSpec
from copytrade_kernel.rulebooks import RulebookCatalog

from .contracts import SYNTHETIC_ACCOUNT
from .evidence import decimal, digest


@dataclass(frozen=True)
class ChallengeRulebookSpec(KernelChallengeSpec):
    @property
    def evidence_sha(self) -> str:
        return digest(asdict(self))

    def to_rulebook_fields(
        self,
        *,
        account_binding: str = SYNTHETIC_ACCOUNT,
        status: str = "SYNTHETIC",
        internal_daily_stop: str | None = None,
        valid_until_ms: int = 10**15,
        scale: str | int | Decimal = 1,
        evidence_sha: str = "",
    ) -> dict[str, Any]:
        return super().to_rulebook_fields(
            account_binding=account_binding,
            status=status,
            internal_daily_stop=internal_daily_stop,
            valid_until_ms=valid_until_ms,
            scale=scale,
            evidence_sha=evidence_sha or self.evidence_sha,
        )

    def to_summary_dict(self) -> dict[str, Any]:
        """Compact agent-friendly summary including the materialized synthetic Rulebook SHA."""
        rb = self.to_rulebook_fields()
        return {
            "key": self.key,
            "firm_slug": self.firm_slug,
            "firm_name": self.firm_name,
            "program": self.program,
            "phase": self.phase,
            "execution_venue": self.execution_venue,
            "initial": rb["initial"],
            "target": rb["target"],
            "daily_loss": rb["daily_loss"],
            "trailing_loss": rb["trailing_loss"],
            "internal_daily_stop": rb["internal_daily_stop"],
            "daily_basis": rb["daily_basis"],
            "trailing_basis": rb["trailing_basis"],
            "trailing_locks_at_initial": self.trailing_locks_at_initial,
            "reset_time_hhmm": self.reset_time_hhmm,
            "min_trading_days": self.min_trading_days,
            "consistency_max_single_day_pct": self.consistency_max_single_day_pct,
            "payout_split_pct": self.payout_split_pct,
            "rulebook_sha": digest(rb),
        }


def _fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def get_challenge_spec(key: str) -> ChallengeRulebookSpec:
    return ChallengeRulebookSpec(**asdict(RulebookCatalog.get(key)))


def list_challenge_keys(firm_slug: str | None = None) -> tuple[str, ...]:
    return RulebookCatalog.list_keys(firm_slug)


def list_challenge_specs(firm_slug: str | None = None) -> tuple[ChallengeRulebookSpec, ...]:
    return tuple(get_challenge_spec(key) for key in list_challenge_keys(firm_slug))


CHALLENGE_RULEBOOKS = {spec.key: spec for spec in list_challenge_specs()}


def hyrotrader_policy(
    base_policy: dict[str, Any],
    *,
    initial: str | Decimal = "5000",
) -> dict[str, Any]:
    """Overlay HyroTrader non-financial policy guards onto a risk_policy_v1 dict."""
    initial_dec = decimal(initial)
    if initial_dec <= 0:
        raise ValueError("RULEBOOK_AMOUNTS")
    policy = dict(base_policy)
    policy["allowed_instrument_types"] = ["usdt_perp"]
    policy["blocked_symbols"] = ["EUR/USD", "EURUSD", "EUR/USD:USDT", "EUR/USDT:USDT"]
    policy["low_cap_mcap_threshold_usd"] = "100000000"
    policy["low_cap_max_notional"] = _fmt_decimal(initial_dec * Decimal("0.05"))
    return policy


def cft_policy(base_policy: dict[str, Any]) -> dict[str, Any]:
    """Overlay Crypto Fund Trader (Bybit) non-financial policy guards onto risk_policy_v1."""
    policy = dict(base_policy)
    policy["allowed_instrument_types"] = ["usdt_perp"]
    return policy


def propr_policy(base_policy: dict[str, Any]) -> dict[str, Any]:
    """Return risk_policy_v1 copy for Propr (multi-asset; no USDT-perp-only restriction)."""
    return dict(base_policy)


def challenge_policy(
    firm_slug: str,
    base_policy: dict[str, Any],
    *,
    initial: str | Decimal = "5000",
) -> dict[str, Any]:
    """Overlay firm-specific non-financial policy guards onto a risk_policy_v1 dict."""
    slug = firm_slug.strip().lower()
    if slug == "hyrotrader":
        return hyrotrader_policy(base_policy, initial=initial)
    if slug == "crypto-fund-trader":
        return cft_policy(base_policy)
    if slug == "propr":
        return propr_policy(base_policy)
    return dict(base_policy)


def consistency_progress(
    day_profits: dict[str, str | Decimal],
    *,
    target: str | Decimal,
    max_single_day_pct: int | None,
) -> dict[str, Any]:
    """Evaluate single-day profit distribution cap (e.g. HyroTrader 40% eval rule)."""
    target_dec = decimal(target)
    if target_dec < 0:
        raise ValueError("RULEBOOK_AMOUNTS")
    cap_per_day = (
        target_dec * Decimal(max_single_day_pct) / Decimal("100")
        if max_single_day_pct is not None and target_dec > 0
        else None
    )
    total_profit = Decimal("0")
    eligible_profit = Decimal("0")
    capped_days: list[str] = []
    for day in sorted(day_profits):
        pnl = decimal(day_profits[day])
        total_profit += pnl
        if cap_per_day is not None and pnl > cap_per_day:
            eligible_profit += cap_per_day
            capped_days.append(day)
        else:
            eligible_profit += pnl
    return {
        "total_profit": _fmt_decimal(total_profit),
        "eligible_profit": _fmt_decimal(eligible_profit),
        "target": _fmt_decimal(target_dec),
        "cap_per_day": _fmt_decimal(cap_per_day) if cap_per_day is not None else None,
        "capped_days": capped_days,
        "target_met": target_dec > 0 and eligible_profit >= target_dec,
    }
