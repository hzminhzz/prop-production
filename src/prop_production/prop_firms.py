"""Canonical challenge firm rulebook catalog for Guardian enforcement & portfolio research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .contracts import SYNTHETIC_ACCOUNT
from .evidence import decimal, digest


def _fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class ChallengeRulebookSpec:
    """Canonical prop-firm challenge specification derived from trading-ops/prop_firms_rules.json."""

    key: str
    firm_slug: str
    firm_name: str
    program: str
    phase: str
    execution_venue: str
    initial: str
    target: str
    daily_loss: str
    trailing_loss: str
    daily_basis: str
    trailing_basis: str
    trailing_locks_at_initial: bool | None = None
    reset_time_hhmm: str = "00:00"
    min_trading_days: int | str | None = None
    consistency_max_single_day_pct: int | None = None
    payout_split_pct: str | None = None
    max_leverage: str = "UNKNOWN"
    prohibited_strategies: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()

    @property
    def evidence_sha(self) -> str:
        """Deterministic SHA-256 over the canonical challenge specification."""
        return digest(asdict(self))

    def to_rulebook_fields(
        self,
        *,
        account_binding: str = SYNTHETIC_ACCOUNT,
        status: str = "SYNTHETIC",
        internal_daily_stop: str | None = None,
        valid_until_ms: int = 10**15,
        scale: str | int = 1,
    ) -> dict[str, Any]:
        """Return exact Rulebook constructor fields (optionally scaled from $5,000 base)."""
        factor = decimal(scale)
        if factor <= 0:
            raise ValueError("RULEBOOK_SCALE_POSITIVE")
        scaled_initial = _fmt_decimal(decimal(self.initial) * factor)
        scaled_target = _fmt_decimal(decimal(self.target) * factor)
        scaled_daily = _fmt_decimal(decimal(self.daily_loss) * factor)
        scaled_trailing = _fmt_decimal(decimal(self.trailing_loss) * factor)
        if internal_daily_stop is None:
            internal_stop = _fmt_decimal(decimal(scaled_daily) * Decimal("0.8"))
        else:
            internal_stop = _fmt_decimal(decimal(internal_daily_stop))
        return {
            "account_binding": account_binding,
            "status": status,
            "initial": scaled_initial,
            "target": scaled_target,
            "daily_loss": scaled_daily,
            "trailing_loss": scaled_trailing,
            "internal_daily_stop": internal_stop,
            "daily_basis": self.daily_basis,
            "trailing_basis": self.trailing_basis,
            "timezone": "UTC",
            "valid_until_ms": valid_until_ms,
            "evidence_sha": self.evidence_sha,
        }

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


_NORTHBOOK_PROHIBITED = (
    "Cross-account hedging",
    "Coordinated group trading",
    "Account sharing, sale or transfer",
    "Identity fraud",
    "Exploiting platform or venue defects",
    "Latency arbitrage and tick sniping",
    "Account cycling",
    "Wash and simulated trading",
    "Insider information and front-running",
    "High-frequency trading (quote stuffing, order-book layering)",
)
_NORTHBOOK_OPEN = (
    "max_leverage: Per-pair leverage caps only shown inside trading terminal.",
    "copytrading_or_ea_allowed: Own-account copy trading permitted; API key sharing and HFT prohibited.",
    "Rulebook status: v0.5 draft, effective 'TBD - at launch'.",
)
_NORTHBOOK_URLS = (
    "https://northbook.xyz/",
    "https://northbook.xyz/rulebook",
    "https://northbook.xyz/rulebook/how-it-works",
    "https://northbook.xyz/rulebook/rules",
    "https://northbook.xyz/rulebook/legal",
)

_VANTA_PROHIBITED = (
    "Third-party signals or copy trading",
    "Mirroring or opposing positions across your own accounts",
    "Martingale detection (Grow, Pro, and Instant Funded with Pro challenge)",
)
_VANTA_OPEN = (
    "daily_loss_limit.reset_time_hhmm: Rules specify 'UTC day' without explicit HH:MM timestamp.",
    "daily_loss_limit.basis (funded): Classic Scaled uses start-of-day balance; Grow/Pro/Instant Funded use start-of-day equity.",
    "max_drawdown.type (funded): Classic Scaled uses 5% STATIC; Grow/Pro/Instant Funded use EOD_TRAILING.",
    "copytrading_or_ea_allowed: Own-strategy EAs/bots allowed; third-party signals or copy trading prohibited.",
)
_VANTA_URLS = (
    "https://www.vantatrading.io/rules",
    "https://www.vantatrading.io/faq",
    "https://www.vantatrading.io/terms-of-service",
    "https://www.vantatrading.io/pricing",
)

_HYRO_PROHIBITED = (
    "Spot trading, USDC perpetuals, COIN-M futures and options",
    "Trading the EUR/USD pair (Bybit challenge)",
    "Using the Martingale strategy",
    "Hedging across multiple accounts",
    "Holding > 5% of initial balance (including leverage) in low-cap coins (mcap < $100M, 24h vol $500K-$5M, or Innovation Zone)",
    "Requesting additional demo funds or modifying USDT balances",
    "Deleting or altering the API key (Bybit challenge)",
    "Combining spot and margin trading within the same account",
    "Copy trading, account mirroring, external signal providers, and ready-made bots",
    "Latency/arbitrage exploits or ultra-fast mass orders exploiting simulation",
)
_HYRO_OPEN = (
    "Minimum trading days conflict: Trading Rules list 4+4 days (Two-Step), FAQ states 5 per phase.",
    "Funded payout cap: Single payout covers up to 5% profit of starting balance with excess cancelled upon payout.",
    "Bybit demo bot fill flagging: Unrealistic demo fills may be flagged (40% target credit).",
)
_HYRO_URLS = (
    "https://www.hyrotrader.com/",
    "https://www.hyrotrader.com/trading-rules/",
    "https://www.hyrotrader.com/faq/",
    "https://www.hyrotrader.com/terms-and-conditions/",
)

_CFT_PROHIBITED = (
    "High-frequency trading (HFT)",
    "Tick scalping (manual or automated)",
    "Arbitrage / latency exploitation EAs",
    "News scalping EAs",
    "Multi-account reverse trading EAs",
    "Reverse trading (simultaneous opposite positions >= 60s across asset/accounts within 24h)",
    "Gambling / all-in trading (> $10,000 simulated profit per day or per trade)",
    "Spot trading on Bybit sub-account",
    "Trading USDC pairs on Bybit (USDT pairs only)",
    "Adding extra funds or modifying/replacing Bybit API key",
)
_CFT_OPEN = (
    "min_trading_days (Evaluation): FAQ states 5 different days unless No Minimum Trading Days add-on; joincft states 0.",
    "Conditional Stop Loss & <=2% risk rule applies on Final Stage for reset/50% payout review.",
)
_CFT_URLS = (
    "https://cryptofundtrader.com/faq/",
    "https://cryptofundtrader.com/evaluation-rules/",
    "https://cryptofundtrader.com/terms-and-conditions/",
    "https://cryptofundtrader.com/choose-your-challenge/",
)

_HYPERNOVA_PROHIBITED = (
    "Cross-account hedging and arbitrage",
    "Copy trading, signal services, and off-the-shelf strategies",
    "Account sharing or multi-account access without prior approval",
    "Exploiting platform errors, latency, or pricing anomalies",
    "Identity fraud",
    "Insider trading and front-running",
    "Strategy switching between assessment and funded account",
    "Strategies that cannot be replicated in live markets",
)
_HYPERNOVA_OPEN = (
    "Rulebook version drift: docs/rulebook v1.4 is current vs older v1.2 homepage.",
    "Homepage vs Rulebook drawdown wording: v1.4 explicitly enforces equity-based intraday breach.",
    "New-trader payout delay duration not specified in Rulebook v1.4.",
)
_HYPERNOVA_URLS = (
    "https://hypernova.xyz/docs/rulebook",
    "https://hypernova.xyz/rulebook",
    "https://hypernova.xyz/",
    "https://hypernova.xyz/markets",
)

_PROPR_PROHIBITED = (
    "Exploiting platform bugs or glitches",
    "Account sharing or selling",
    "Identity fraud",
    "Opposite hedging across accounts",
    "Hedging across external CEX or DEX accounts",
    "Third-party coordination",
    "Account cycling / high-frequency eval purchases",
    "Exploiting the payout/drawdown interaction",
    "Latency arbitrage and tick sniping",
    "Simulated or wash trading",
    "Sharing account access or multi-account usage from same location",
    "Non-replicable or high-risk strategies",
)
_PROPR_OPEN = (
    "Max open positions: Homepage card states 6; /rules states no max position size rule beyond leverage limits.",
    "Funded 1-Step Max Drawdown for Turbo/Pro: 3% static (Turbo) / 5% static (Pro) vs summary 6%.",
)
_PROPR_URLS = (
    "https://www.propr.xyz/",
    "https://www.propr.xyz/rules",
    "https://www.propr.xyz/faq",
)


def _specs() -> tuple[ChallengeRulebookSpec, ...]:
    return (
        # Northbook ($5,000)
        ChallengeRulebookSpec(
            key="northbook:1step_base:phase_1",
            firm_slug="northbook",
            firm_name="Northbook",
            program="1-Step Base",
            phase="phase_1",
            execution_venue="Simulated accounts priced from Hyperliquid (on-chain USDC settlement)",
            initial="5000",
            target="450",
            daily_loss="150",
            trailing_loss="150",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            prohibited_strategies=_NORTHBOOK_PROHIBITED,
            open_questions=_NORTHBOOK_OPEN,
            source_urls=_NORTHBOOK_URLS,
        ),
        ChallengeRulebookSpec(
            key="northbook:1step_base:funded",
            firm_slug="northbook",
            firm_name="Northbook",
            program="1-Step Base",
            phase="funded",
            execution_venue="Simulated accounts priced from Hyperliquid (on-chain USDC settlement)",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="150",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            payout_split_pct="80% (90% with Split 90 add-on)",
            prohibited_strategies=_NORTHBOOK_PROHIBITED,
            open_questions=_NORTHBOOK_OPEN,
            source_urls=_NORTHBOOK_URLS,
        ),
        ChallengeRulebookSpec(
            key="northbook:1step_classic:phase_1",
            firm_slug="northbook",
            firm_name="Northbook",
            program="1-Step Classic",
            phase="phase_1",
            execution_venue="Simulated accounts priced from Hyperliquid (on-chain USDC settlement)",
            initial="5000",
            target="500",
            daily_loss="150",
            trailing_loss="300",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            prohibited_strategies=_NORTHBOOK_PROHIBITED,
            open_questions=_NORTHBOOK_OPEN,
            source_urls=_NORTHBOOK_URLS,
        ),
        ChallengeRulebookSpec(
            key="northbook:1step_classic:funded",
            firm_slug="northbook",
            firm_name="Northbook",
            program="1-Step Classic",
            phase="funded",
            execution_venue="Simulated accounts priced from Hyperliquid (on-chain USDC settlement)",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="300",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            payout_split_pct="80% (90% with Split 90 add-on)",
            prohibited_strategies=_NORTHBOOK_PROHIBITED,
            open_questions=_NORTHBOOK_OPEN,
            source_urls=_NORTHBOOK_URLS,
        ),
        ChallengeRulebookSpec(
            key="northbook:1step_prime:phase_1",
            firm_slug="northbook",
            firm_name="Northbook",
            program="1-Step Prime",
            phase="phase_1",
            execution_venue="Simulated accounts priced from Hyperliquid (on-chain USDC settlement)",
            initial="5000",
            target="500",
            daily_loss="150",
            trailing_loss="400",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            prohibited_strategies=_NORTHBOOK_PROHIBITED,
            open_questions=_NORTHBOOK_OPEN,
            source_urls=_NORTHBOOK_URLS,
        ),
        ChallengeRulebookSpec(
            key="northbook:1step_prime:funded",
            firm_slug="northbook",
            firm_name="Northbook",
            program="1-Step Prime",
            phase="funded",
            execution_venue="Simulated accounts priced from Hyperliquid (on-chain USDC settlement)",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="400",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            payout_split_pct="80% (90% with Split 90 add-on)",
            prohibited_strategies=_NORTHBOOK_PROHIBITED,
            open_questions=_NORTHBOOK_OPEN,
            source_urls=_NORTHBOOK_URLS,
        ),
        # Vanta Trading ($5,000)
        ChallengeRulebookSpec(
            key="vanta-trading:classic_1step_3pct:phase_1",
            firm_slug="vanta-trading",
            firm_name="Vanta Trading",
            program="Classic (1-Step Evaluation, 3% daily)",
            phase="phase_1",
            execution_venue="Vanta Trading Desk (Subnet 8; priced from Hyperliquid, Databento, Massive)",
            initial="5000",
            target="500",
            daily_loss="150",
            trailing_loss="250",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            reset_time_hhmm="UNKNOWN",
            max_leverage="Portfolio 40x | Forex 30x | Crypto 10x | Indices 8x | Commodities 6x | Equities 4x",
            prohibited_strategies=_VANTA_PROHIBITED[:2],
            open_questions=_VANTA_OPEN,
            source_urls=_VANTA_URLS,
        ),
        ChallengeRulebookSpec(
            key="vanta-trading:classic_1step_5pct:phase_1",
            firm_slug="vanta-trading",
            firm_name="Vanta Trading",
            program="Classic (1-Step Evaluation, 5% daily)",
            phase="phase_1",
            execution_venue="Vanta Trading Desk (Subnet 8; priced from Hyperliquid, Databento, Massive)",
            initial="5000",
            target="500",
            daily_loss="250",
            trailing_loss="250",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            reset_time_hhmm="UNKNOWN",
            max_leverage="Portfolio 40x | Forex 30x | Crypto 10x | Indices 8x | Commodities 6x | Equities 4x",
            prohibited_strategies=_VANTA_PROHIBITED[:2],
            open_questions=_VANTA_OPEN,
            source_urls=_VANTA_URLS,
        ),
        ChallengeRulebookSpec(
            key="vanta-trading:classic_scaled:funded",
            firm_slug="vanta-trading",
            firm_name="Vanta Trading",
            program="Classic Scaled",
            phase="funded",
            execution_venue="Vanta Trading Desk (Subnet 8; priced from Hyperliquid, Databento, Massive)",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="250",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            trailing_locks_at_initial=False,
            reset_time_hhmm="UNKNOWN",
            payout_split_pct="100/0 default (60%-100%)",
            max_leverage="Classic: 40x portfolio",
            prohibited_strategies=_VANTA_PROHIBITED[:2],
            open_questions=_VANTA_OPEN,
            source_urls=_VANTA_URLS,
        ),
        ChallengeRulebookSpec(
            key="vanta-trading:grow:funded",
            firm_slug="vanta-trading",
            firm_name="Vanta Trading",
            program="Grow",
            phase="funded",
            execution_venue="Vanta Trading Desk (Subnet 8; priced from Hyperliquid, Databento, Massive)",
            initial="5000",
            target="300",
            daily_loss="250",
            trailing_loss="400",
            daily_basis="EQUITY",
            trailing_basis="EOD_EQUITY",
            reset_time_hhmm="UNKNOWN",
            min_trading_days=90,
            consistency_max_single_day_pct=20,
            payout_split_pct="60%-100%",
            max_leverage="Pro/Grow: 50x portfolio (Forex 40x, Crypto 12x, Indices 10x, Commodities 8x, Equities 6x)",
            prohibited_strategies=_VANTA_PROHIBITED,
            open_questions=_VANTA_OPEN,
            source_urls=_VANTA_URLS,
        ),
        ChallengeRulebookSpec(
            key="vanta-trading:pro:funded",
            firm_slug="vanta-trading",
            firm_name="Vanta Trading",
            program="Pro",
            phase="funded",
            execution_venue="Vanta Trading Desk (Subnet 8; priced from Hyperliquid, Databento, Massive)",
            initial="5000",
            target="0",
            daily_loss="250",
            trailing_loss="400",
            daily_basis="EQUITY",
            trailing_basis="EOD_EQUITY",
            reset_time_hhmm="UNKNOWN",
            payout_split_pct="100/0 default (60%-100%)",
            max_leverage="Pro/Grow: 50x portfolio (Forex 40x, Crypto 12x, Indices 10x, Commodities 8x, Equities 6x)",
            prohibited_strategies=_VANTA_PROHIBITED,
            open_questions=_VANTA_OPEN,
            source_urls=_VANTA_URLS,
        ),
        ChallengeRulebookSpec(
            key="vanta-trading:instant_funded_5pct:funded",
            firm_slug="vanta-trading",
            firm_name="Vanta Trading",
            program="Instant Funded (5% EOD)",
            phase="funded",
            execution_venue="Vanta Trading Desk (Subnet 8; priced from Hyperliquid, Databento, Massive)",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="250",
            daily_basis="EQUITY",
            trailing_basis="EOD_EQUITY",
            reset_time_hhmm="UNKNOWN",
            payout_split_pct="80/20 fixed",
            max_leverage="Classic/Instant: 40x-50x portfolio",
            prohibited_strategies=_VANTA_PROHIBITED,
            open_questions=_VANTA_OPEN,
            source_urls=_VANTA_URLS,
        ),
        ChallengeRulebookSpec(
            key="vanta-trading:instant_funded_8pct:funded",
            firm_slug="vanta-trading",
            firm_name="Vanta Trading",
            program="Instant Funded (8% EOD)",
            phase="funded",
            execution_venue="Vanta Trading Desk (Subnet 8; priced from Hyperliquid, Databento, Massive)",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="400",
            daily_basis="EQUITY",
            trailing_basis="EOD_EQUITY",
            reset_time_hhmm="UNKNOWN",
            payout_split_pct="80/20 fixed",
            max_leverage="Classic/Instant: 40x-50x portfolio",
            prohibited_strategies=_VANTA_PROHIBITED,
            open_questions=_VANTA_OPEN,
            source_urls=_VANTA_URLS,
        ),
        # HyroTrader ($5,000 Two-Step)
        ChallengeRulebookSpec(
            key="hyrotrader:two_step:phase_1",
            firm_slug="hyrotrader",
            firm_name="HyroTrader",
            program="Two-Step",
            phase="phase_1",
            execution_venue="Bybit (Demo API), BloFin (Demo API), Hyro, Tealstreet, Tiger.com — USDT Perpetual Futures only",
            initial="5000",
            target="500",
            daily_loss="250",
            trailing_loss="500",
            daily_basis="EQUITY",
            trailing_basis="STATIC",
            min_trading_days=4,
            consistency_max_single_day_pct=40,
            max_leverage="50x-100x on most pairs (Bybit); up to 150x on BTC/ETH (BloFin)",
            prohibited_strategies=_HYRO_PROHIBITED,
            open_questions=_HYRO_OPEN,
            source_urls=_HYRO_URLS,
        ),
        ChallengeRulebookSpec(
            key="hyrotrader:two_step:phase_2",
            firm_slug="hyrotrader",
            firm_name="HyroTrader",
            program="Two-Step",
            phase="phase_2",
            execution_venue="Bybit (Demo API), BloFin (Demo API), Hyro, Tealstreet, Tiger.com — USDT Perpetual Futures only",
            initial="5000",
            target="250",
            daily_loss="250",
            trailing_loss="500",
            daily_basis="EQUITY",
            trailing_basis="STATIC",
            min_trading_days=4,
            consistency_max_single_day_pct=40,
            max_leverage="50x-100x on most pairs (Bybit); up to 150x on BTC/ETH (BloFin)",
            prohibited_strategies=_HYRO_PROHIBITED,
            open_questions=_HYRO_OPEN,
            source_urls=_HYRO_URLS,
        ),
        ChallengeRulebookSpec(
            key="hyrotrader:two_step:funded",
            firm_slug="hyrotrader",
            firm_name="HyroTrader",
            program="Two-Step",
            phase="funded",
            execution_venue="Bybit (Demo API), BloFin (Demo API), Hyro, Tealstreet, Tiger.com — USDT Perpetual Futures only",
            initial="5000",
            target="0",
            daily_loss="250",
            trailing_loss="500",
            daily_basis="EQUITY",
            trailing_basis="STATIC",
            payout_split_pct="80",
            max_leverage="50x-100x on most pairs (Bybit); up to 150x on BTC/ETH (BloFin)",
            prohibited_strategies=_HYRO_PROHIBITED,
            open_questions=_HYRO_OPEN,
            source_urls=_HYRO_URLS,
        ),
        # Crypto Fund Trader ($5,000 2-Phase Evaluation)
        ChallengeRulebookSpec(
            key="crypto-fund-trader:two_phase:phase_1",
            firm_slug="crypto-fund-trader",
            firm_name="Crypto Fund Trader",
            program="2-Phase Evaluation",
            phase="phase_1",
            execution_venue="Bybit",
            initial="5000",
            target="400",
            daily_loss="250",
            trailing_loss="500",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            reset_time_hhmm="00:05",
            min_trading_days="AMBIGUOUS",
            max_leverage="100",
            prohibited_strategies=_CFT_PROHIBITED,
            open_questions=_CFT_OPEN,
            source_urls=_CFT_URLS,
        ),
        ChallengeRulebookSpec(
            key="crypto-fund-trader:two_phase:phase_2",
            firm_slug="crypto-fund-trader",
            firm_name="Crypto Fund Trader",
            program="2-Phase Evaluation",
            phase="phase_2",
            execution_venue="Bybit",
            initial="5000",
            target="250",
            daily_loss="250",
            trailing_loss="500",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            reset_time_hhmm="00:05",
            min_trading_days="AMBIGUOUS",
            max_leverage="100",
            prohibited_strategies=_CFT_PROHIBITED,
            open_questions=_CFT_OPEN,
            source_urls=_CFT_URLS,
        ),
        ChallengeRulebookSpec(
            key="crypto-fund-trader:two_phase:funded",
            firm_slug="crypto-fund-trader",
            firm_name="Crypto Fund Trader",
            program="2-Phase Evaluation",
            phase="funded",
            execution_venue="Bybit",
            initial="5000",
            target="0",
            daily_loss="250",
            trailing_loss="500",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            reset_time_hhmm="00:05",
            min_trading_days=15,
            payout_split_pct="80",
            max_leverage="100",
            prohibited_strategies=_CFT_PROHIBITED,
            open_questions=_CFT_OPEN,
            source_urls=_CFT_URLS,
        ),
        # Hypernova ($5,000 1-Step Assessment: Tight, Low, Medium, High Risk)
        ChallengeRulebookSpec(
            key="hypernova:1step_tight:phase_1",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (Tight Risk)",
            phase="phase_1",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="450",
            daily_loss="150",
            trailing_loss="150",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        ChallengeRulebookSpec(
            key="hypernova:1step_tight:funded",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (Tight Risk)",
            phase="funded",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="150",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            payout_split_pct="80% standard (90% with add-on)",
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        ChallengeRulebookSpec(
            key="hypernova:1step_low:phase_1",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (Low Risk)",
            phase="phase_1",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="500",
            daily_loss="150",
            trailing_loss="300",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        ChallengeRulebookSpec(
            key="hypernova:1step_low:funded",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (Low Risk)",
            phase="funded",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="300",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            payout_split_pct="80% standard (90% with add-on)",
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        ChallengeRulebookSpec(
            key="hypernova:1step_medium:phase_1",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (Medium Risk)",
            phase="phase_1",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="500",
            daily_loss="200",
            trailing_loss="350",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        ChallengeRulebookSpec(
            key="hypernova:1step_medium:funded",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (Medium Risk)",
            phase="funded",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="0",
            daily_loss="200",
            trailing_loss="350",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            payout_split_pct="80% standard (90% with add-on)",
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        ChallengeRulebookSpec(
            key="hypernova:1step_high:phase_1",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (High Risk)",
            phase="phase_1",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="500",
            daily_loss="250",
            trailing_loss="400",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        ChallengeRulebookSpec(
            key="hypernova:1step_high:funded",
            firm_slug="hypernova",
            firm_name="Hypernova",
            program="1-Step Assessment (High Risk)",
            phase="funded",
            execution_venue="Hypernova internal engine priced from Hyperliquid market data",
            initial="5000",
            target="0",
            daily_loss="250",
            trailing_loss="400",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            min_trading_days=0,
            payout_split_pct="80% standard",
            max_leverage="2x-10x per symbol (v1.4) | 5x homepage table",
            prohibited_strategies=_HYPERNOVA_PROHIBITED,
            open_questions=_HYPERNOVA_OPEN,
            source_urls=_HYPERNOVA_URLS,
        ),
        # Propr ($5,000 Classic 1-Step, Turbo 1-Step, Pro 1-Step, Classic 2-Step)
        ChallengeRulebookSpec(
            key="propr:classic_1step:phase_1",
            firm_slug="propr",
            firm_name="Propr",
            program="Classic 1-Step",
            phase="phase_1",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="500",
            daily_loss="150",
            trailing_loss="300",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:classic_1step:funded",
            firm_slug="propr",
            firm_name="Propr",
            program="Classic 1-Step",
            phase="funded",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="300",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            payout_split_pct="80",
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:turbo_1step:phase_1",
            firm_slug="propr",
            firm_name="Propr",
            program="Turbo 1-Step",
            phase="phase_1",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="450",
            daily_loss="150",
            trailing_loss="150",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:turbo_1step:funded",
            firm_slug="propr",
            firm_name="Propr",
            program="Turbo 1-Step",
            phase="funded",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="150",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            payout_split_pct="80",
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:pro_1step:phase_1",
            firm_slug="propr",
            firm_name="Propr",
            program="Pro 1-Step",
            phase="phase_1",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="600",
            daily_loss="150",
            trailing_loss="250",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:pro_1step:funded",
            firm_slug="propr",
            firm_name="Propr",
            program="Pro 1-Step",
            phase="funded",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="0",
            daily_loss="150",
            trailing_loss="250",
            daily_basis="BALANCE",
            trailing_basis="STATIC",
            payout_split_pct="80",
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:classic_2step:phase_1",
            firm_slug="propr",
            firm_name="Propr",
            program="Classic 2-Step",
            phase="phase_1",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="250",
            daily_loss="250",
            trailing_loss="400",
            daily_basis="BALANCE",
            trailing_basis="INTRADAY_EQUITY",
            trailing_locks_at_initial=True,
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:classic_2step:phase_2",
            firm_slug="propr",
            firm_name="Propr",
            program="Classic 2-Step",
            phase="phase_2",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="500",
            daily_loss="250",
            trailing_loss="400",
            daily_basis="BALANCE",
            trailing_basis="INTRADAY_EQUITY",
            trailing_locks_at_initial=True,
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
        ChallengeRulebookSpec(
            key="propr:classic_2step:funded",
            firm_slug="propr",
            firm_name="Propr",
            program="Classic 2-Step",
            phase="funded",
            execution_venue="100% simulated accounts; hybrid A-Book (Hyperliquid onchain) and B-Book",
            initial="5000",
            target="0",
            daily_loss="250",
            trailing_loss="400",
            daily_basis="BALANCE",
            trailing_basis="INTRADAY_EQUITY",
            trailing_locks_at_initial=True,
            payout_split_pct="80",
            max_leverage="25x FX; 10x BTC/ETH/SOL, SP500, XYZ100; 8x Gold/Silver/CL/Brent; 5x Other; 4x Equities; 2x Other Crypto",
            prohibited_strategies=_PROPR_PROHIBITED,
            open_questions=_PROPR_OPEN,
            source_urls=_PROPR_URLS,
        ),
    )


CHALLENGE_RULEBOOKS: dict[str, ChallengeRulebookSpec] = {spec.key: spec for spec in _specs()}


def get_challenge_spec(key: str) -> ChallengeRulebookSpec:
    """Return the canonical ChallengeRulebookSpec for a firm:program:phase key."""
    try:
        return CHALLENGE_RULEBOOKS[key]
    except KeyError as exc:
        raise ValueError(f"UNKNOWN_CHALLENGE_RULEBOOK:{key}") from exc


def list_challenge_keys(firm_slug: str | None = None) -> tuple[str, ...]:
    """List canonical challenge rulebook keys, optionally filtered by firm_slug."""
    return tuple(spec.key for spec in list_challenge_specs(firm_slug=firm_slug))


def list_challenge_specs(firm_slug: str | None = None) -> tuple[ChallengeRulebookSpec, ...]:
    """List canonical ChallengeRulebookSpec entries, optionally filtered by firm_slug."""
    if firm_slug is None:
        return tuple(CHALLENGE_RULEBOOKS.values())
    slug = firm_slug.strip().lower()
    return tuple(spec for spec in CHALLENGE_RULEBOOKS.values() if spec.firm_slug == slug)


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
