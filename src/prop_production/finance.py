"""Exact account-level contractual floors; broker financial equity is the authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from copytrade_kernel.accounting import DAY_MS as DAY_MS
from copytrade_kernel.accounting import AccountValuation
from copytrade_kernel.accounting import Rulebook as KernelRulebook
from copytrade_kernel.accounting import account_step as kernel_account_step
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import SYNTHETIC_ACCOUNT
from .evidence import digest, verify
from .gateway import BrokerSnapshot


@dataclass(frozen=True)
class Rulebook(KernelRulebook):
    @classmethod
    def synthetic(cls) -> Rulebook:
        return cls(
            SYNTHETIC_ACCOUNT,
            "SYNTHETIC",
            valid_until_ms=10**15,
            evidence_sha=digest("invented rulebook; not provider terms"),
        )

    @classmethod
    def from_challenge(
        cls,
        key: str,
        *,
        account_binding: str = SYNTHETIC_ACCOUNT,
        status: str = "SYNTHETIC",
        internal_daily_stop: str | None = None,
        valid_until_ms: int = 10**15,
        scale: str | int = 1,
    ) -> Rulebook:
        """Build an exact Rulebook from a canonical challenge firm spec."""
        from .prop_firms import get_challenge_spec

        fields = get_challenge_spec(key).to_rulebook_fields(
            account_binding=account_binding,
            status=status,
            internal_daily_stop=internal_daily_stop,
            valid_until_ms=valid_until_ms,
            scale=scale,
        )
        return cls(**fields)

    @classmethod
    def list_challenges(cls, firm_slug: str | None = None) -> tuple[str, ...]:
        """List canonical challenge rulebook keys, optionally filtered by firm_slug."""
        from .prop_firms import list_challenge_keys

        return list_challenge_keys(firm_slug=firm_slug)

    @property
    def sha(self) -> str:
        return digest(asdict(self))

    @classmethod
    def authenticated(
        cls, envelope: dict[str, Any], key: Ed25519PublicKey, account_binding: str, now_ms: int
    ) -> Rulebook:
        body = verify(envelope, key, "rulebook-reviewer")
        rules = cls(**body)
        if rules.account_binding != account_binding or rules.status != "AUTHENTICATED":
            raise ValueError("RULEBOOK_ACCOUNT_OR_STATUS")
        rules.validate(now_ms)
        return rules


def account_step(
    previous: dict[str, Any], snapshot: BrokerSnapshot, rules: Rulebook, now_ms: int
) -> tuple[dict[str, Any], list[str]]:
    valuation = AccountValuation(
        snapshot.account_binding,
        snapshot.observed_at_ms,
        snapshot.equity,
        snapshot.cash,
        snapshot.unrealized,
        snapshot.initial_margin,
        snapshot.maintenance_margin,
        snapshot.ready(now_ms, 5000),
    )
    return kernel_account_step(previous, valuation, rules, now_ms, rulebook_sha=rules.sha)
