"""Exact account-level contractual floors; broker financial equity is the authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import SYNTHETIC_ACCOUNT
from .evidence import decimal, digest, verify
from .gateway import BrokerSnapshot

DAY_MS = 86400000


@dataclass(frozen=True)
class Rulebook:
    account_binding: str
    status: str
    initial: str = "10000"
    target: str = "1000"
    daily_loss: str = "500"
    trailing_loss: str = "1000"
    internal_daily_stop: str = "400"
    daily_basis: str = "MAX_BALANCE_EQUITY"
    trailing_basis: str = "INTRADAY_EQUITY"
    timezone: str = "UTC"
    valid_until_ms: int = 0
    evidence_sha: str = ""

    @classmethod
    def synthetic(cls) -> Rulebook:
        return cls(SYNTHETIC_ACCOUNT, "SYNTHETIC", valid_until_ms=10**15,
                   evidence_sha=digest("invented rulebook; not provider terms"))

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
    def authenticated(cls, envelope: dict[str, Any], key: Ed25519PublicKey,
                      account_binding: str, now_ms: int) -> Rulebook:
        body = verify(envelope, key, "rulebook-reviewer")
        rules = cls(**body)
        if rules.account_binding != account_binding or rules.status != "AUTHENTICATED":
            raise ValueError("RULEBOOK_ACCOUNT_OR_STATUS")
        rules.validate(now_ms)
        return rules

    def validate(self, now_ms: int) -> None:
        if (self.timezone != "UTC"
                or self.daily_basis not in {"EQUITY", "BALANCE", "MAX_BALANCE_EQUITY"}
                or self.trailing_basis not in {"INTRADAY_EQUITY", "EOD_EQUITY", "STATIC"}
                or type(self.valid_until_ms) is not int or now_ms >= self.valid_until_ms
                or not self.evidence_sha):
            raise ValueError("RULEBOOK_UNSUPPORTED_OR_EXPIRED")
        if decimal(self.target) < 0 or any(
            decimal(value) <= 0
            for value in (self.initial, self.daily_loss, self.trailing_loss, self.internal_daily_stop)
        ):
            raise ValueError("RULEBOOK_AMOUNTS")
        if decimal(self.internal_daily_stop) > decimal(self.daily_loss):
            raise ValueError("RULEBOOK_INTERNAL_STOP")


def account_step(previous: dict[str, Any], snapshot: BrokerSnapshot, rules: Rulebook,
                 now_ms: int) -> tuple[dict[str, Any], list[str]]:
    if snapshot.account_binding != rules.account_binding:
        raise ValueError("RULEBOOK_ACCOUNT")
    reasons = []
    if rules.status not in {"SYNTHETIC", "AUTHENTICATED"}:
        reasons.append("RULEBOOK_UNVERIFIED")
    try:
        rules.validate(now_ms)
    except ValueError as exc:
        reasons.append(str(exc))
    if not snapshot.ready(now_ms, 5000):
        return dict(previous), reasons + ["BROKER_FINANCE_UNKNOWN"]
    assert snapshot.equity is not None and snapshot.cash is not None
    equity, cash = snapshot.equity, snapshot.cash
    day = snapshot.observed_at_ms // DAY_MS
    state = dict(previous)
    if state and state["rulebook_sha"] != rules.sha:
        return state, reasons + ["RULEBOOK_CHANGED_REQUIRES_REVIEW"]
    if state and snapshot.observed_at_ms < state["observed_at_ms"]:
        return state, reasons + ["BROKER_CLOCK_REGRESSION"]
    if rules.daily_basis == "EQUITY":
        baseline = equity
    elif rules.daily_basis == "BALANCE":
        baseline = cash
    else:
        baseline = max(cash, equity)
    initial = decimal(rules.initial)
    if not state:
        # Bootstrapping a real account needs its authenticated beginning-of-day baseline.
        # Synthetic fixtures define inception at this first observation.
        if rules.status != "SYNTHETIC" and snapshot.observed_at_ms % DAY_MS != 0:
            return {}, reasons + ["MIDNIGHT_BASELINE_UNKNOWN"]
        initial_hwm = initial if rules.trailing_basis == "STATIC" else max(initial, equity)
        state = {"day": day, "daily_baseline": str(baseline),
                 "high_water": str(initial_hwm), "day_high_water": str(equity),
                 "realized_made": "0", "rulebook_sha": rules.sha}
    elif day != state["day"]:
        if day != state["day"] + 1 or snapshot.observed_at_ms % DAY_MS != 0:
            reasons.append("MIDNIGHT_BASELINE_UNKNOWN")
        else:
            if rules.trailing_basis == "EOD_EQUITY":
                state["high_water"] = str(max(decimal(state["high_water"]), equity))
            state.update(day=day, daily_baseline=str(baseline),
                         day_high_water=str(equity), realized_made="0")
    if rules.trailing_basis == "INTRADAY_EQUITY":
        state["high_water"] = str(max(decimal(state["high_water"]), equity))
    state["day_high_water"] = str(max(decimal(state["day_high_water"]), equity))
    made = max(decimal(state["realized_made"]), decimal(state["day_high_water"]) - equity)
    if rules.trailing_basis == "STATIC":
        trailing_floor = initial - decimal(rules.trailing_loss)
    else:
        trailing_floor = max(
            initial - decimal(rules.trailing_loss),
            decimal(state["high_water"]) - decimal(rules.trailing_loss),
        )
    state.update(
        cash=str(cash), equity=str(equity), unrealized=str(snapshot.unrealized),
        initial_margin=str(snapshot.initial_margin), maintenance_margin=str(snapshot.maintenance_margin),
        realized_made=str(made), observed_at_ms=snapshot.observed_at_ms,
        daily_floor=str(decimal(state["daily_baseline"]) - decimal(rules.daily_loss)),
        internal_floor=str(decimal(state["daily_baseline"]) - decimal(rules.internal_daily_stop)),
        trailing_floor=str(trailing_floor),
    )
    for field, reason in (("daily_floor", "CONTRACT_DAILY_FLOOR"),
                          ("internal_floor", "INTERNAL_DAILY_STOP"),
                          ("trailing_floor", "TRAILING_FLOOR")):
        if equity <= decimal(state[field]):
            reasons.append(reason)
    state["risk_floor"] = str(max(decimal(state[k]) for k in
                                 ("daily_floor", "internal_floor", "trailing_floor")))
    state["headroom"] = str(max(decimal("0"), equity - decimal(state["risk_floor"])))
    return state, reasons
