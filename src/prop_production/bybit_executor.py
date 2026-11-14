"""5m CWSA-to-BybitDemoGateway execution consumer for prop challenges."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from .bybit_gateway import BybitDemoGateway
from .evidence import decimal, digest
from .gateway import BrokerSnapshot
from .prop_firms import get_challenge_spec

_ZERO = Decimal("0")

TRIADA_HYDRO5K_K: dict[str, Decimal] = {
    "ACT04": Decimal("0.00030"),
    "ACT08": Decimal("0.00237"),
    "ACT09": Decimal("0.00066"),
}


@dataclass(frozen=True)
class MarketSpec:
    """Minimal Bybit linear USDT perp market constraints."""

    symbol: str
    min_amount: Decimal
    amount_step: Decimal


def _utc_day_key(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _quantize_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= _ZERO:
        return value
    sign = Decimal("-1") if value < _ZERO else Decimal("1")
    units = (abs(value) / step).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return sign * units * step


def load_cwsa_targets(
    source_db: Path,
    member_k: dict[str, Decimal],
) -> tuple[str, int, dict[str, Decimal]]:
    """Read latest CWSA 5m wave for all portfolio members from read-only SQLite."""
    uri = f"file:{source_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        rows = conn.execute("SELECT stream, scope, state FROM cwsa_heads").fetchall()
    finally:
        conn.close()

    by_source: dict[str, tuple[str, int, list[dict[str, Any]]]] = {}
    for _stream, scope_raw, state_raw in rows:
        scope = json.loads(scope_raw)
        sid = scope.get("source_id")
        if sid not in member_k:
            continue
        state = json.loads(state_raw)
        seq = int(state["sequence"])
        next_cp = int(state["next_checkpoint_ms"])
        econ_sha = str(state["economic_sha"])
        by_source[sid] = (f"{seq}:{next_cp}:{econ_sha}", next_cp, list(state["positions"]))

    missing = sorted(set(member_k) - set(by_source))
    if missing:
        raise ValueError(f"CWSA_MEMBERS_MISSING:{','.join(missing)}")

    next_cps = {v[1] for v in by_source.values()}
    if len(next_cps) != 1:
        raise ValueError("CWSA_CHECKPOINT_SKEW")
    next_cp_ms = next(iter(next_cps))

    net: dict[str, Decimal] = {}
    wave_parts: dict[str, str] = {}
    for sid in sorted(member_k):
        sig, _cp, positions = by_source[sid]
        wave_parts[sid] = sig
        k = member_k[sid]
        for pos in positions:
            coin = str(pos["instrument"]).split("/")[-1]
            qty = decimal(pos["quantity"])
            net[coin] = net.get(coin, _ZERO) + qty * k

    wave_key = digest({"next_cp_ms": next_cp_ms, "sources": wave_parts})
    return wave_key, next_cp_ms, {c: q for c, q in sorted(net.items()) if q != _ZERO}


def _reduce_only_for_delta(current_qty: Decimal, delta_qty: Decimal) -> bool:
    if current_qty > _ZERO and delta_qty < _ZERO:
        return abs(delta_qty) <= current_qty
    if current_qty < _ZERO and delta_qty > _ZERO:
        return abs(delta_qty) <= abs(current_qty)
    return False


class BybitCwsaExecutor:
    """Durable, fail-closed 5m CWSA-to-BybitDemoGateway executor."""

    def __init__(
        self,
        *,
        state_db: Path,
        source_db: Path,
        member_k: dict[str, Decimal],
        rulebook_key: str = "hyrotrader:two_step:phase_1",
        internal_daily_stop_usd: Decimal = Decimal("200"),
    ) -> None:
        self._state_db = state_db
        self._source_db = source_db
        self._member_k = dict(member_k)
        self._spec = get_challenge_spec(rulebook_key)
        self._static_floor = decimal(self._spec.initial) - decimal(self._spec.trailing_loss)
        self._internal_daily_stop = internal_daily_stop_usd
        self._state_db.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._state_db, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_baselines (
                    day_utc TEXT PRIMARY KEY,
                    start_equity TEXT NOT NULL,
                    recorded_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_waves (
                    wave_key TEXT PRIMARY KEY,
                    next_cp_ms INTEGER NOT NULL,
                    processed_at_ms INTEGER NOT NULL,
                    intents_count INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    wave_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    reduce_only INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    broker_id TEXT,
                    reason TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )

    def _day_baseline(self, day_utc: str, equity: Decimal, now_ms: int) -> Decimal:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT start_equity FROM daily_baselines WHERE day_utc = ?", (day_utc,)
            ).fetchone()
            if row is not None:
                return decimal(row[0])
            conn.execute(
                "INSERT INTO daily_baselines (day_utc, start_equity, recorded_at_ms) "
                "VALUES (?, ?, ?)",
                (day_utc, str(equity), now_ms),
            )
            return equity

    def _wave_processed(self, wave_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_waves WHERE wave_key = ?", (wave_key,)
            ).fetchone()
            return row is not None

    def _record_intent_pending(
        self, intent_id: str, wave_key: str, symbol: str, qty: Decimal, reduce_only: bool, now_ms: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO order_intents "
                "(intent_id, wave_key, symbol, quantity, reduce_only, status, broker_id, reason, "
                "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, 'PENDING_SUBMIT', NULL, NULL, ?, ?)",
                (intent_id, wave_key, symbol, str(qty), int(reduce_only), now_ms, now_ms),
            )

    def _update_intent_outcome(
        self, intent_id: str, status: str, broker_id: str | None, reason: str | None, now_ms: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE order_intents SET status = ?, broker_id = ?, reason = ?, updated_at_ms = ? "
                "WHERE intent_id = ?",
                (status, broker_id, reason, now_ms, intent_id),
            )

    def _mark_wave_processed(self, wave_key: str, next_cp_ms: int, now_ms: int, count: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_waves "
                "(wave_key, next_cp_ms, processed_at_ms, intents_count) VALUES (?, ?, ?, ?)",
                (wave_key, next_cp_ms, now_ms, count),
            )

    def _risk_check(self, snap: BrokerSnapshot, now_ms: int) -> str | None:
        if not snap.financial_complete or snap.uncertainty_reasons:
            reasons = ",".join(snap.uncertainty_reasons) or "INCOMPLETE"
            return f"BLOCKED_RECONCILIATION:{reasons}"
        if snap.equity is None:
            return "BLOCKED_EQUITY_MISSING"
        if snap.equity <= self._static_floor:
            return f"HALT_STATIC_DD:equity={snap.equity}<={self._static_floor}"
        day_start = self._day_baseline(_utc_day_key(now_ms), snap.equity, now_ms)
        if day_start - snap.equity >= self._internal_daily_stop:
            return f"HALT_DAILY_STOP:loss={day_start - snap.equity}>={self._internal_daily_stop}"
        return None

    def step(
        self,
        gateway: Any,
        markets: dict[str, MarketSpec],
        now_ms: int,
        *,
        execute: bool = False,
    ) -> dict[str, Any]:
        """Run one reconcile -> target -> risk -> dispatch cycle."""
        snap: BrokerSnapshot = gateway.observe(now_ms)
        blocker = self._risk_check(snap, now_ms)
        if blocker is not None:
            return {"status": "BLOCKED", "reason": blocker, "submitted": 0}

        wave_key, next_cp_ms, coin_targets = load_cwsa_targets(self._source_db, self._member_k)
        if self._wave_processed(wave_key):
            return {"status": "ALREADY_PROCESSED", "wave_key": wave_key, "submitted": 0}

        current_by_symbol: dict[str, Decimal] = {p.market: p.quantity for p in snap.positions}
        open_order_symbols = {o.market for o in snap.orders}

        planned: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []

        # Build target map by Bybit symbol; include open positions not in targets (target=0 to close).
        target_by_symbol: dict[str, Decimal] = {}
        for coin, qty in coin_targets.items():
            spec = markets.get(coin) or markets.get(f"1000{coin}")
            if spec is None:
                skipped.append({"coin": coin, "reason": "UNSUPPORTED_BYBIT_USDT_PERP"})
                continue
            scaled = qty * Decimal("1000") if spec.symbol.startswith("1000") and not coin.startswith("1000") else qty
            target_by_symbol[spec.symbol] = target_by_symbol.get(spec.symbol, _ZERO) + scaled

        all_symbols = sorted(set(target_by_symbol) | set(current_by_symbol))
        spec_by_symbol = {s.symbol: s for s in markets.values()}
        for sym in all_symbols:
            if sym == "EUR/USD" or not sym.endswith("/USDT:USDT"):
                skipped.append({"symbol": sym, "reason": "HYRO_POLICY_BLOCKED"})
                continue
            if sym in open_order_symbols:
                skipped.append({"symbol": sym, "reason": "OPEN_ORDER_PENDING"})
                continue
            spec = spec_by_symbol.get(sym)
            if spec is None:
                skipped.append({"symbol": sym, "reason": "MISSING_MARKET_SPEC"})
                continue
            raw_delta = target_by_symbol.get(sym, _ZERO) - current_by_symbol.get(sym, _ZERO)
            qty_step = _quantize_step(raw_delta, spec.amount_step)
            if abs(qty_step) < spec.min_amount:
                continue
            reduce_only = _reduce_only_for_delta(current_by_symbol.get(sym, _ZERO), qty_step)
            intent_id = f"hy-{wave_key[:12]}-{sym.split('/')[0][:12]}"[:36]
            planned.append(
                {
                    "intent_id": intent_id,
                    "market": sym,
                    "quantity": str(qty_step),
                    "order_type": "market",
                    "reduce_only": reduce_only,
                }
            )

        if not execute:
            return {
                "status": "SHADOW_DRY_RUN",
                "wave_key": wave_key,
                "next_cp_ms": next_cp_ms,
                "planned_count": len(planned),
                "planned": planned,
                "skipped": skipped,
                "submitted": 0,
            }

        results: list[dict[str, Any]] = []
        for intent in planned:
            self._record_intent_pending(
                intent["intent_id"],
                wave_key,
                intent["market"],
                decimal(intent["quantity"]),
                bool(intent["reduce_only"]),
                now_ms,
            )
            outcome = gateway.submit(intent)
            self._update_intent_outcome(
                intent["intent_id"], outcome.status, outcome.broker_id, outcome.reason, now_ms
            )
            results.append(
                {
                    "intent_id": intent["intent_id"],
                    "market": intent["market"],
                    "quantity": intent["quantity"],
                    "status": outcome.status,
                    "broker_id": outcome.broker_id,
                    "reason": outcome.reason,
                }
            )

        self._mark_wave_processed(wave_key, next_cp_ms, now_ms, len(results))
        return {
            "status": "EXECUTED",
            "wave_key": wave_key,
            "next_cp_ms": next_cp_ms,
            "submitted": len(results),
            "results": results,
            "skipped": skipped,
        }


def _build_markets(exchange: Any) -> dict[str, MarketSpec]:
    out: dict[str, MarketSpec] = {}
    for sym, m in getattr(exchange, "markets", {}).items():
        if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")):
            continue
        base = str(m.get("base") or "")
        min_raw = m.get("limits", {}).get("amount", {}).get("min")
        step_raw = m.get("precision", {}).get("amount")
        min_amt = decimal(str(min_raw) if min_raw is not None else "0")
        step = decimal(str(step_raw) if step_raw is not None else "0")
        if base:
            out[base] = MarketSpec(symbol=sym, min_amount=min_amt, amount_step=step)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db",
        type=Path,
        default=Path("/home/quant/.local/share/copytrade/prospective/cwsa-5m-source-chapter2-20261112/paper-events.sqlite3"),
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=Path("/var/lib/prop-production-hydro/executor.sqlite"),
    )
    parser.add_argument("--env-prefix", default="BYBIT_HYDRO")
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    from connectors.bybit.client import BybitClient
    from connectors.bybit.config import BybitConfig, Environment, Mode
    from connectors.bybit.reconcile import BybitDemoReconciler

    prefix = args.env_prefix.strip()
    uid = os.environ[f"{prefix}_EXPECTED_UID"]
    mode = Mode.EXECUTION if args.execute else Mode.READ_ONLY
    cfg = BybitConfig(
        environment=Environment.DEMO,
        mode=mode,
        api_key=os.environ[f"{prefix}_API_KEY"],
        api_secret=os.environ[f"{prefix}_API_SECRET"],
        expected_demo_uid=uid,
    )
    client = BybitClient(cfg)
    reconciler = BybitDemoReconciler(client)
    executor = BybitCwsaExecutor(
        state_db=args.state_db,
        source_db=args.source_db,
        member_k=TRIADA_HYDRO5K_K,
    )

    with BybitDemoGateway(reconciler, expected_demo_uid=uid) as gateway:
        markets = _build_markets(client.exchange)
        while True:
            now_ms = int(time.time() * 1000)
            report = executor.step(gateway, markets, now_ms, execute=args.execute)
            print(json.dumps(report, sort_keys=True), flush=True)
            if args.once:
                break
            time.sleep(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
