"""Persistent hidden broker truth. No real endpoint, credential or socket exists here."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..contracts import SYNTHETIC_ACCOUNT, SYNTHETIC_ENDPOINT
from ..evidence import canonical, decimal, digest
from ..gateway import BrokerOrder, BrokerSnapshot, Execution, Outcome, Position
from ..ipc import LocalServer
from ..worker import sandbox

ZERO = Decimal("0")


class FakeBroker:
    synthetic = True
    endpoint = SYNTHETIC_ENDPOINT

    def __init__(self, path: Path):
        self.con = sqlite3.connect(path, isolation_level=None, timeout=0.1)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.execute("CREATE TABLE IF NOT EXISTS truth (id INTEGER PRIMARY KEY, payload TEXT)")
        row = self.con.execute("SELECT payload FROM truth WHERE id=1").fetchone()
        self.__truth: dict[str, Any] = (
            json.loads(row[0])
            if row
            else {
                "cash": "10000",
                "positions": {},
                "orders": {},
                "fills": [],
                "audit": [],
                "ledger": [],
                "epoch": 1,
                "clock": 0,
                "marks": {"BTC": "10000", "ETH": "1000"},
            }
        )
        self._save()
        self.connected = True
        self.history_complete = True
        self.stale_by_ms = 0
        self.fault_mode = "NONE"
        self.fill_on_submit = True
        self.maintenance_rate = Decimal("0.01")
        self.execution_position_mode = "ONE_WAY"
        self.cross_zero_supported = True
        self.reduce_only_supported = True

    def _save(self) -> None:
        self.con.execute(
            "INSERT INTO truth VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (canonical(self.__truth).decode(),),
        )

    def audit(self) -> list[dict[str, Any]]:
        return list(json.loads(canonical(self.__truth["audit"])))

    def ledger(self) -> list[dict[str, Any]]:
        return list(json.loads(canonical(self.__truth["ledger"])))

    def submit(self, intent: dict[str, Any]) -> Outcome:
        if not self.connected:
            raise TimeoutError("FAKE_PROVIDER_UNREACHABLE")
        if intent["account_binding"] != SYNTHETIC_ACCOUNT or intent["endpoint"] != SYNTHETIC_ENDPOINT:
            raise PermissionError("SYNTHETIC_ACCOUNT_ONLY")
        key = intent["intent_id"]
        wire_fields = (
            "intent_id",
            "account_binding",
            "endpoint",
            "market",
            "quantity",
            "limit_price",
            "reduce_only",
            "decision_id",
            "release_sha",
        )
        wire_sha = digest({k: intent[k] for k in wire_fields})
        existing = self.__truth["orders"].get(key)
        if existing:
            if existing["wire_sha"] != wire_sha:
                raise ValueError("INTENT_PAYLOAD_CONFLICT")
            return Outcome(existing["status"], existing["broker_id"])
        self.__truth["audit"].append(
            {
                "kind": "SUBMIT",
                "intent_id": key,
                "wire_sha": wire_sha,
                "commit_sequence": intent["commit_sequence"],
                "commit_sha": intent["commit_sha"],
                "known_at_ms": self.__truth["clock"],
            }
        )
        quantity = decimal(intent["quantity"])
        if quantity == 0:
            raise ValueError("ZERO_ORDER")
        broker_id = "fake-" + key[:24]
        status = "REJECTED" if self.fault_mode == "REJECT" else "ACCEPTED"
        self.__truth["orders"][key] = dict(
            intent, wire_sha=wire_sha, broker_id=broker_id, filled="0", status=status
        )
        self.__truth["epoch"] += 1
        self._save()
        if status != "REJECTED" and self.fill_on_submit:
            mark = decimal(self.__truth["marks"][intent["market"]])
            price = mark * (Decimal("1.003") if quantity > 0 else Decimal("0.997"))
            limit = decimal(intent["limit_price"])
            if (quantity > 0 and price <= limit) or (quantity < 0 and price >= limit):
                self.fill(key, "1", str(price))
        if self.fault_mode == "ACCEPT_THEN_TIMEOUT":
            raise TimeoutError("ACCEPTED_THEN_HTTP_TIMEOUT")
        return Outcome(status, broker_id)

    def fill(self, intent_id: str, fraction: str = "1", price: str | None = None) -> None:
        order = self.__truth["orders"][intent_id]
        if order["status"] in {"FILLED", "REJECTED", "CANCELLED", "EXPIRED"}:
            return
        factor = decimal(fraction)
        if not 0 < factor <= 1:
            raise ValueError("FILL_FRACTION")
        quantity = (decimal(order["quantity"]) - decimal(order["filled"])) * factor
        market = order["market"]
        old = self.__truth["positions"].get(market, {"quantity": "0", "entry_price": "0"})
        old_q, entry = decimal(old["quantity"]), decimal(old["entry_price"])
        if order["reduce_only"]:
            if old_q * quantity >= 0:
                order["status"] = "REJECTED"
                self._save()
                return
            quantity = max(quantity, -old_q) if quantity < 0 else min(quantity, -old_q)
        execution_price = decimal(price or self.__truth["marks"][market])
        fee = abs(quantity * execution_price) * Decimal("0.001")
        closed = min(abs(old_q), abs(quantity)) if old_q * quantity < 0 else ZERO
        realized = closed * (execution_price - entry) * (1 if old_q > 0 else -1)
        new_q = old_q + quantity
        if old_q * quantity >= 0:
            new_entry = (abs(old_q) * entry + abs(quantity) * execution_price) / abs(new_q)
        elif new_q == 0:
            new_entry = ZERO
        elif old_q * new_q < 0:
            new_entry = execution_price
        else:
            new_entry = entry
        self.__truth["positions"][market] = {"quantity": str(new_q), "entry_price": str(new_entry)}
        self.__truth["cash"] = str(decimal(self.__truth["cash"]) + realized - fee)
        fill = {
            "execution_id": "exec-" + str(len(self.__truth["fills"]) + 1),
            "intent_id": intent_id,
            "market": market,
            "quantity": str(quantity),
            "price": str(execution_price),
            "fee": str(fee),
            "known_at_ms": self.__truth["clock"],
        }
        self.__truth["fills"].append(fill)
        self.__truth["ledger"].append(dict(fill, kind="FILL"))
        order["filled"] = str(decimal(order["filled"]) + quantity)
        order["status"] = (
            "FILLED" if decimal(order["filled"]) == decimal(order["quantity"]) else "PARTIAL"
        )
        self.__truth["epoch"] += 1
        self._save()

    def cancel(self, intent_id: str) -> Outcome:
        if not self.connected:
            raise TimeoutError("FAKE_CANCEL_UNREACHABLE")
        order = self.__truth["orders"].get(intent_id)
        if order is None:
            return Outcome("UNKNOWN", reason="NO_SUCH_INTENT_NOT_A_FLAT_PROOF")
        if self.fault_mode == "PARTIAL_DURING_CANCEL":
            self.fill(intent_id, "0.5")
        if order["status"] not in {"FILLED", "REJECTED", "EXPIRED"}:
            order["status"] = "CANCELLED"
        self.__truth["audit"].append(
            {"kind": "CANCEL", "intent_id": intent_id, "known_at_ms": self.__truth["clock"]}
        )
        self.__truth["epoch"] += 1
        self._save()
        if self.fault_mode == "CANCEL_THEN_TIMEOUT":
            raise TimeoutError("CANCEL_OUTCOME_UNKNOWN")
        return Outcome(order["status"], order["broker_id"])

    def mark(self, market: str, price: str, now_ms: int) -> None:
        if decimal(price) <= 0:
            raise ValueError("PRICE")
        self.__truth["marks"][market] = price
        self.__truth["clock"] = now_ms
        self.__truth["ledger"].append(
            {"kind": "MARK", "market": market, "price": price, "known_at_ms": now_ms}
        )
        self.__truth["epoch"] += 1
        self._save()

    def funding(self, amount: str, now_ms: int) -> None:
        self.__truth["cash"] = str(decimal(self.__truth["cash"]) + decimal(amount))
        self.__truth["clock"] = now_ms
        self.__truth["ledger"].append({"kind": "FUNDING", "amount": amount, "known_at_ms": now_ms})
        self.__truth["epoch"] += 1
        self._save()

    def inject_position(self, market: str, quantity: str, entry_price: str) -> None:
        # External/orphan inventory: deliberately has no application intent or fill history.
        self.__truth["positions"][market] = {"quantity": quantity, "entry_price": entry_price}
        self.__truth["epoch"] += 1
        self._save()

    def observe(self, now_ms: int) -> BrokerSnapshot:
        if not self.connected:
            raise TimeoutError("FAKE_PROVIDER_UNREACHABLE")
        self.__truth["clock"] = now_ms
        self._save()
        positions = tuple(
            Position(
                m,
                decimal(p["quantity"]),
                decimal(p["entry_price"]),
                decimal(self.__truth["marks"][m]),
            )
            for m, p in sorted(self.__truth["positions"].items())
            if decimal(p["quantity"])
        )
        unrealized = sum((p.quantity * (p.mark_price - p.entry_price) for p in positions), ZERO)
        gross = sum((abs(p.quantity * p.mark_price) for p in positions), ZERO)
        cash = decimal(self.__truth["cash"])
        orders = tuple(
            BrokerOrder(
                o["intent_id"],
                o["broker_id"],
                o["market"],
                decimal(o["quantity"]),
                decimal(o["filled"]),
                o["status"],
                o["reduce_only"],
            )
            for o in self.__truth["orders"].values()
        )
        fills = tuple(
            Execution(
                f["execution_id"],
                f["intent_id"],
                f["market"],
                decimal(f["quantity"]),
                decimal(f["price"]),
                decimal(f["fee"]),
                f["known_at_ms"],
            )
            for f in self.__truth["fills"]
        )

        def fact(feature: str, supported: bool = True) -> dict[str, Any]:
            return {
                "status": "SYNTHETIC" if supported else "UNKNOWN",
                "evidence_sha": digest(["independent-fixture-v1", feature]) if supported else None,
                "provenance": "independent-fixture-transport-v1" if supported else None,
            }

        # The independent fixture emits wire evidence; it imports neither the
        # optional native SDK nor the Guardian's acceptance implementation.
        capabilities = tuple(
            {
                "schema": "native_execution_capabilities_v1",
                "account": SYNTHETIC_ACCOUNT,
                "endpoint": SYNTHETIC_ENDPOINT,
                "instrument": market,
                "observed_at_ms": max(0, now_ms - self.stale_by_ms),
                "known_at_ms": now_ms,
                "valid_until_ms": now_ms + 2000,
                "position_mode": self.execution_position_mode,
                "net_order": fact("net_order"),
                "reduce_only": fact("reduce_only", self.reduce_only_supported),
                "cross_zero": fact("cross_zero", self.cross_zero_supported),
                "trading_authorized": False,
            }
            for market in self.__truth["marks"]
        )
        return BrokerSnapshot(
            SYNTHETIC_ACCOUNT,
            SYNTHETIC_ENDPOINT,
            max(0, now_ms - self.stale_by_ms),
            now_ms,
            self.__truth["epoch"],
            cash,
            cash + unrealized,
            unrealized,
            gross * Decimal("0.1"),
            gross * self.maintenance_rate,
            positions,
            orders,
            fills,
            cutoff=f"fake-genesis-through-{self.__truth['epoch']}"
            if self.history_complete
            else None,
            history_complete=self.history_complete,
            connected=True,
            financial_complete=True,
            market_marks=tuple(
                (m, decimal(price), max(0, now_ms - self.stale_by_ms))
                for m, price in sorted(self.__truth["marks"].items())
            ),
            execution_capabilities=capabilities,
        )

    def close(self) -> None:
        self.con.close()


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(v) for v in value]
    return value


def broker_worker(root: Path) -> None:
    broker = FakeBroker(root / "broker" / "truth.sqlite")
    roles = {
        "guardian": (
            os.getuid(),
            (root / "broker" / "guardian.key").read_bytes(),
            {"observe", "submit", "cancel"},
        ),
        "judge": (
            os.getuid(),
            (root / "broker" / "judge.key").read_bytes(),
            {"observe", "fault", "mark", "funding", "ledger", "audit", "fill", "stop"},
        ),
    }
    server = LocalServer(root / "broker" / "b.sock", roles)
    running = True

    def stop(*args: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)

    def handle(role: str, body: dict[str, Any]) -> dict[str, Any]:
        command = body["command"]
        try:
            if command == "observe":
                return {"snapshot": _wire(asdict(broker.observe(body["now_ms"])))}
            if command == "submit":
                return {"outcome": asdict(broker.submit(body["intent"]))}
            if command == "cancel":
                return {"outcome": asdict(broker.cancel(body["intent_id"]))}
            if command == "fault":
                allowed = {
                    "fault_mode",
                    "connected",
                    "history_complete",
                    "stale_by_ms",
                    "fill_on_submit",
                }
                if set(body) - allowed - {"command"}:
                    raise ValueError("FAULT_SCHEMA")
                for k, v in body.items():
                    if k != "command":
                        setattr(broker, k, v)
                return {"configured": True}
            if command == "mark":
                broker.mark(body["market"], body["price"], body["now_ms"])
                return {"marked": True}
            if command == "funding":
                broker.funding(body["amount"], body["now_ms"])
                return {"funding": True}
            if command == "fill":
                broker.fill(body["intent_id"], body["fraction"], body.get("price"))
                return {"filled": True}
            if command == "ledger":
                return {"ledger": broker.ledger()}
            if command == "audit":
                return {"audit": broker.audit()}
            if command == "stop":
                stop()
                return {"stopping": True}
            raise ValueError("COMMAND")
        except TimeoutError:
            return {"unavailable": True}

    sandbox(root, "broker")
    print('{"ready":"SYNTHETIC_BROKER"}', flush=True)
    try:
        while running:
            server.serve_once(handle)
    finally:
        server.close()
        broker.close()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m prop_production.judge.broker STATE_ROOT")
    broker_worker(Path(sys.argv[1]).resolve())


if __name__ == "__main__":
    main()
