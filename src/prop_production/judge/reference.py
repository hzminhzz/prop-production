"""Independent rational arithmetic and protocol specification.

Only the Python standard library is used. Never import production accounting,
risk, journal, planner, transport or fixture calculations into this oracle.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

ZERO = Fraction(0)


def money(value: Fraction) -> Fraction:
    """Declared synthetic currency precision: nearest 1e-8, ties to even."""
    units = value * 100000000
    lower, remainder = divmod(units.numerator, units.denominator)
    if 2 * remainder > units.denominator or (2 * remainder == units.denominator and lower % 2):
        lower += 1
    return Fraction(lower, 100000000)


class ReferenceAccount:
    def __init__(self, initial: str, *, daily_loss: str = "500", trailing_loss: str = "1000",
                 internal_stop: str = "400", maintenance_rate: str = "0.01"):
        self.initial = Fraction(initial)
        self.cash = self.initial
        self.positions: dict[str, tuple[Fraction, Fraction]] = {}
        self.marks: dict[str, Fraction] = {}
        self.fills: dict[str, dict[str, Any]] = {}
        self.daily_loss = Fraction(daily_loss)
        self.trailing_loss = Fraction(trailing_loss)
        self.internal_stop = Fraction(internal_stop)
        self.maintenance_rate = Fraction(maintenance_rate)
        self.day = 0
        self.baseline = self.initial
        self.high_water = self.initial
        self.day_high = self.initial
        self.made = ZERO
        self.baseline_unknown = False

    @property
    def equity(self) -> Fraction:
        return self.cash + sum((quantity * (self.marks.get(m, entry) - entry)
                                for m, (quantity, entry) in self.positions.items()), ZERO)

    def apply(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        if kind == "MARK":
            price = Fraction(event["price"])
            if price <= 0:
                raise ValueError("REFERENCE_PRICE")
            self.marks[event["market"]] = price
        elif kind == "FUNDING":
            self.cash += Fraction(event["amount"])
        elif kind == "FILL":
            identity = event["execution_id"]
            if identity in self.fills:
                if self.fills[identity] != event:
                    raise ValueError("REFERENCE_DUPLICATE_CONFLICT")
                return
            self.fills[identity] = dict(event)
            m = event["market"]
            quantity, price, fee = (Fraction(event[k]) for k in ("quantity", "price", "fee"))
            if quantity == 0 or price <= 0 or fee < 0:
                raise ValueError("REFERENCE_INVALID_FILL")
            old_q, entry = self.positions.get(m, (ZERO, ZERO))
            new_q = old_q + quantity
            realized = ZERO
            if old_q * quantity < 0:
                closed = min(abs(old_q), abs(quantity))
                realized = closed * (price - entry) * (1 if old_q > 0 else -1)
            if new_q == 0:
                new_entry = ZERO
            elif old_q * quantity >= 0:
                new_entry = (abs(old_q) * entry + abs(quantity) * price) / abs(new_q)
            elif old_q * new_q < 0:
                new_entry = price
            else:
                new_entry = entry
            self.positions[m] = (new_q, new_entry)
            self.cash += realized - fee
        else:
            raise ValueError("REFERENCE_EVENT_UNKNOWN")
        self.report(event["known_at_ms"])

    def report(self, now_ms: int) -> dict[str, Any]:
        day = now_ms // 86400000
        if day != self.day:
            if day == self.day + 1 and now_ms % 86400000 == 0:
                self.day = day
                self.baseline = max(self.cash, self.equity)
                self.day_high = self.equity
                self.made = ZERO
            else:
                self.baseline_unknown = True
        self.high_water = max(self.high_water, self.equity)
        self.day_high = max(self.day_high, self.equity)
        self.made = max(self.made, self.day_high - self.equity)
        daily_floor = self.baseline - self.daily_loss
        trailing_floor = max(self.initial - self.trailing_loss, self.high_water - self.trailing_loss)
        breaches = []
        for floor, name in ((daily_floor, "CONTRACT_DAILY_FLOOR"),
                            (trailing_floor, "TRAILING_FLOOR"),
                            (self.baseline - self.internal_stop, "INTERNAL_DAILY_STOP")):
            if self.equity <= floor:
                breaches.append(name)
        gross = sum((abs(quantity * self.marks.get(m, entry))
                     for m, (quantity, entry) in self.positions.items()), ZERO)
        return {"cash": str(money(self.cash)), "equity": str(money(self.equity)),
                "daily_floor": str(daily_floor), "trailing_floor": str(trailing_floor),
                "maintenance_margin": str(money(gross * self.maintenance_rate)),
                "made": str(money(self.made)), "breaches": breaches,
                "baseline_unknown": self.baseline_unknown, "trading_authorized": False}

    def compare(self, observed: dict[str, Any]) -> dict[str, Any]:
        expected = {"cash": money(self.cash), "equity": money(self.equity)}
        for name, value in expected.items():
            if name not in observed or money(Fraction(observed[name])) != value:
                return {"verdict": "FAIL", "counterexample": {"field": name,
                        "expected": str(value), "observed": observed.get(name)}}
        return {"verdict": "EXACT_REFERENCE_MATCH", "currency_quantum": "0.00000001"}


def check_protocol(events: list[dict[str, Any]]) -> dict[str, Any]:
    identities: dict[str, str] = {}
    for index, event in enumerate(events):
        if event["kind"] == "SEND":
            if (not event["committed"] or event["halted"] or event["certainty"] != "COHERENT"
                    or not event["reservation"]):
                return {"verdict": "FAIL", "counterexample": {"index": index,
                        "reason": "UNAUTHORIZED_OR_UNRESERVED_SEND", "event": event}}
            key = event["intent_id"]
            if key in identities:
                return {"verdict": "FAIL", "counterexample": {"index": index,
                        "reason": "DUPLICATE_SEND_WITHOUT_PROVIDER_PROOF", "event": event}}
            identities[key] = event["payload_sha"]
        elif event["kind"] == "FLAT" and (event["positions"] or event["pending"] or not event["coherent"]):
            return {"verdict": "FAIL", "counterexample": {"index": index, "reason": "FALSE_FLAT"}}
    return {"verdict": "PROTOCOL_TRACE_VERIFIED", "sends": len(identities)}


def check_model(depth: int = 10) -> dict[str, Any]:
    """Exhaust the finite one-intent protocol through the declared transition depth.

    Independent abstract model. Actual binary traces are checked separately; this
    model alone does not establish implementation refinement or a live guarantee.
    """
    # intent state, permit, halt, coherent, storage healthy, send count, boot generation
    initial = ("EMPTY", False, False, False, True, 0, 0)
    seen = {initial}
    frontier = {initial}
    transitions = 0
    for _ in range(depth):
        following = set()
        for state in frontier:
            intent, permit, halt, coherent, healthy, sends, boot = state
            candidates = [
                (intent, False, True, coherent, healthy, sends, boot),  # manual halt
                (intent, False, True, False, healthy, sends, boot),  # feed gap
                (intent, False, True, coherent, False, sends, boot),  # storage fault
                ("UNKNOWN" if intent == "SENDING" else intent, False,
                 halt or intent == "SENDING", False, healthy, sends, min(boot + 1, 2)),
            ]
            if healthy:
                candidates.append((intent, not halt and intent in {"EMPTY", "QUEUED"},
                                   halt, True, healthy, sends, boot))
            if permit and healthy and coherent and not halt and intent == "EMPTY":
                candidates.append(("QUEUED", permit, halt, coherent, healthy, sends, boot))
            if permit and healthy and coherent and not halt and intent == "QUEUED" and sends == 0:
                candidates.append(("SENDING", permit, halt, coherent, healthy, 1, boot))
            if intent == "SENDING":
                candidates.extend([("UNKNOWN", False, True, False, healthy, sends, boot),
                                   ("FILLED", False, halt, True, healthy, sends, boot)])
            if intent == "UNKNOWN" and healthy:
                candidates.append(("FILLED", False, halt, True, healthy, sends, boot))
            for candidate in candidates:
                transitions += 1
                if candidate[5] > 1 or (candidate[1] and (candidate[2] or not candidate[3] or not candidate[4])):
                    return {"verdict": "FAIL", "counterexample": [state, candidate]}
                if candidate not in seen:
                    seen.add(candidate)
                    following.add(candidate)
        frontier = following
    return {"verdict": "BOUNDED_PROTOCOL_VERIFIED", "states": len(seen),
            "transitions": transitions, "depth": depth, "max_intents": 1,
            "max_boot_generations": 3, "implementation_refinement": "CHECK_BINARY_TRACES_SEPARATELY"}
