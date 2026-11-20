"""Canonical native instrument and exact fixed-k inputs."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

_NATIVE_KEY = re.compile(r"hyperliquid/mainnet:perp:[^/\s:]+/[^/\s:]+")


def decimal(value: object) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("EXACT_DECIMAL_REQUIRED")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("INVALID_DECIMAL") from exc
    if not result.is_finite():
        raise ValueError("NONFINITE_DECIMAL")
    return result


@dataclass(frozen=True, slots=True, order=True)
class NativeInstrument:
    key: str

    def __post_init__(self) -> None:
        if not self.valid(self.key):
            raise ValueError("INVALID_NATIVE_INSTRUMENT")

    @staticmethod
    def valid(key: object) -> bool:
        return isinstance(key, str) and _NATIVE_KEY.fullmatch(key) is not None


@dataclass(frozen=True, slots=True)
class FixedKSizing:
    k: Decimal
    diagnostic_source_nav: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.k.is_finite() or not 0 < self.k <= 10:
            raise ValueError("MANDATE_GENERATION_K_OR_CLOCK")


def finite(value: Fraction) -> Decimal:
    """Convert a terminating rational without ambient Decimal-context rounding."""
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("NONTERMINATING_ATTRIBUTION_AMOUNT")
    places = max(twos, fives)
    coefficient = value.numerator * 2 ** (places - twos) * 5 ** (places - fives)
    return Decimal((int(coefficient < 0), tuple(map(int, str(abs(coefficient)))), -places))


def add(*values: Decimal) -> Decimal:
    return finite(sum((Fraction(v) for v in values), Fraction(0)))
