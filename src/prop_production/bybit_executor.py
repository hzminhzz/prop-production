"""5m CWSA-to-BybitDemoGateway execution consumer for prop challenges."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from .bybit_gateway import BybitDemoGateway
from .evidence import decimal, digest
from .gateway import BrokerSnapshot, MarketSpec, Outcome
from .prop_firms import ChallengeRulebookSpec, get_challenge_spec

_ZERO = Decimal("0")
_ALLOWED_REST_UNCERTAINTY = frozenset(
    {
        "PROVIDER_CASH_UNVERIFIED",
        "PROVIDER_CUTOFF_UNPROVEN",
        "PROVIDER_TIME_STALE",
        "RECONCILIATION_UNCERTAIN",
        "STREAM_NOT_CONNECTED",
    }
)

TRIADA_HYDRO5K_K: dict[str, Decimal] = {
    "ACT04": Decimal("0.00045"),
    "ACT08": Decimal("0.00356"),
    "ACT09": Decimal("0.00099"),
}


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    if ":" not in value:
        return None
    hh_str, mm_str = value.split(":", 1)
    if not (hh_str.isdigit() and mm_str.isdigit() and len(hh_str) == 2 and len(mm_str) == 2):
        return None
    hh, mm = int(hh_str), int(mm_str)
    if 0 <= hh < 24 and 0 <= mm < 60:
        return hh, mm
    return None


def _utc_day_key(now_ms: int, reset_time_hhmm: str = "00:00") -> str:
    dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    if reset_time_hhmm and reset_time_hhmm.upper() != "UNKNOWN":
        parsed = _parse_hhmm(reset_time_hhmm)
        if parsed is not None:
            dt = dt - timedelta(hours=parsed[0], minutes=parsed[1])
    return dt.strftime("%Y-%m-%d")


def _quantize_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= _ZERO:
        return value
    sign = Decimal("-1") if value < _ZERO else Decimal("1")
    units = (abs(value) / step).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return sign * units * step


def _load_cwsa_snapshot(
    source_db: Path,
    member_k: dict[str, Decimal],
) -> tuple[str, int, dict[str, dict[str, Decimal]], dict[str, str]]:
    """Read latest CWSA 5m wave per source from read-only SQLite."""
    uri = f"file:{source_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        rows = conn.execute("SELECT stream, scope, state FROM cwsa_heads").fetchall()
    finally:
        conn.close()

    by_source: dict[str, tuple[str, int, str, dict[str, Decimal]]] = {}
    for _stream, scope_raw, state_raw in rows:
        scope = json.loads(scope_raw)
        sid = scope.get("source_id")
        if sid not in member_k:
            continue
        state = json.loads(state_raw)
        seq = int(state["sequence"])
        next_cp = int(state["next_checkpoint_ms"])
        econ_sha = str(state["economic_sha"])
        phase = str(state.get("phase") or "CONTINUOUS")
        pos_map: dict[str, Decimal] = {}
        for pos in state["positions"]:
            coin = str(pos["instrument"]).split("/")[-1]
            qty = decimal(pos["quantity"])
            if qty != _ZERO:
                pos_map[coin] = pos_map.get(coin, _ZERO) + qty
        by_source[sid] = (f"{seq}:{next_cp}:{econ_sha}", next_cp, phase, pos_map)

    missing = sorted(set(member_k) - set(by_source))
    if missing:
        raise ValueError(f"CWSA_MEMBERS_MISSING:{','.join(missing)}")

    next_cps = {v[1] for v in by_source.values()}
    if len(next_cps) != 1:
        raise ValueError("CWSA_CHECKPOINT_SKEW")
    next_cp_ms = next(iter(next_cps))

    wave_parts: dict[str, str] = {}
    positions_by_source: dict[str, dict[str, Decimal]] = {}
    phases: dict[str, str] = {}
    for sid in sorted(member_k):
        sig, _cp, phase, pos_map = by_source[sid]
        wave_parts[sid] = sig
        phases[sid] = phase
        positions_by_source[sid] = pos_map

    wave_key = digest(
        {
            "next_cp_ms": next_cp_ms,
            "sources": wave_parts,
            "k": {sid: str(k) for sid, k in sorted(member_k.items())},
        }
    )
    return wave_key, next_cp_ms, positions_by_source, phases


def load_cwsa_targets(
    source_db: Path,
    member_k: dict[str, Decimal],
) -> tuple[str, int, dict[str, Decimal]]:
    """Read latest netted CWSA 5m wave for all portfolio members from read-only SQLite."""
    wave_key, next_cp_ms, positions_by_source, _phases = _load_cwsa_snapshot(source_db, member_k)
    net: dict[str, Decimal] = {}
    for sid, pos_map in positions_by_source.items():
        k = member_k[sid]
        for coin, qty in pos_map.items():
            net[coin] = net.get(coin, _ZERO) + qty * k
    return wave_key, next_cp_ms, {c: q for c, q in sorted(net.items()) if q != _ZERO}


def _symbol_for_coin(coin: str, markets: dict[str, MarketSpec]) -> MarketSpec | None:
    return markets.get(coin) or markets.get(f"1000{coin}")


def _reduce_only_for_delta(current_qty: Decimal, delta_qty: Decimal) -> bool:
    if current_qty > _ZERO and delta_qty < _ZERO:
        return abs(delta_qty) <= current_qty
    if current_qty < _ZERO and delta_qty > _ZERO:
        return abs(delta_qty) <= abs(current_qty)
    return False


def _symbol_mark_price(
    sym: str, snap: BrokerSnapshot, spec: MarketSpec
) -> Decimal | None:
    """Return best finite positive mark price for sym from broker state or market spec."""
    for pos in snap.positions:
        if pos.market == sym:
            for px in (pos.mark_price, pos.entry_price):
                if px.is_finite() and px > _ZERO:
                    return px
    for mkt, px, _known_ms in snap.market_marks:
        if mkt == sym and px.is_finite() and px > _ZERO:
            return px
    if spec.reference_price is not None and spec.reference_price.is_finite() and spec.reference_price > _ZERO:
        return spec.reference_price
    return None


class BybitCwsaExecutor:
    """Durable, fail-closed 5m CWSA-to-BrokerGateway executor driven by prop rulebooks."""

    def __init__(
        self,
        *,
        state_db: Path,
        source_db: Path,
        member_k: dict[str, Decimal],
        rulebook_key: str = "hyrotrader:two_step:phase_1",
        internal_daily_stop_usd: Decimal | None = None,
        max_source_lag_ms: int | None = 600_000,
        allow_rest_reconciliation: bool = False,
        min_trade_notional_usd: Decimal = _ZERO,
        jitter_min_seconds: int = 0,
        jitter_max_seconds: int = 0,
        jitter_seed: str | None = None,
        reset_time_hhmm: str | None = None,
        day_start_equity_usd: Decimal | None = None,
    ) -> None:
        if not min_trade_notional_usd.is_finite() or min_trade_notional_usd < _ZERO:
            raise ValueError("INVALID_MIN_TRADE_NOTIONAL")
        if jitter_min_seconds < 0 or jitter_max_seconds < jitter_min_seconds:
            raise ValueError("INVALID_JITTER_RANGE")
        jitter_max_ms = jitter_max_seconds * 1000
        if max_source_lag_ms is not None and jitter_max_ms > 0 and jitter_max_ms >= max_source_lag_ms:
            raise ValueError("JITTER_EXCEEDS_MAX_SOURCE_LAG")
        self._state_db = state_db
        self._source_db = source_db
        self._member_k = dict(member_k)
        self._spec: ChallengeRulebookSpec = get_challenge_spec(rulebook_key)
        effective_reset = (
            reset_time_hhmm.strip()
            if reset_time_hhmm is not None and reset_time_hhmm.strip()
            else self._spec.reset_time_hhmm
        )
        if effective_reset.upper() != "UNKNOWN" and _parse_hhmm(effective_reset) is None:
            raise ValueError("INVALID_RESET_TIME_HHMM")
        if day_start_equity_usd is not None and (
            not day_start_equity_usd.is_finite() or day_start_equity_usd <= _ZERO
        ):
            raise ValueError("INVALID_DAY_START_EQUITY")
        self._reset_time_hhmm = effective_reset
        self._day_start_equity_override = day_start_equity_usd
        self._initial = decimal(self._spec.initial)
        self._trailing_loss = decimal(self._spec.trailing_loss)
        self._static_floor = self._initial - self._trailing_loss
        self._internal_daily_stop = (
            internal_daily_stop_usd
            if internal_daily_stop_usd is not None
            else decimal(self._spec.daily_loss) * Decimal("0.8")
        )
        self._max_source_lag_ms = max_source_lag_ms
        self._allow_rest_reconciliation = allow_rest_reconciliation
        self._min_trade_notional = min_trade_notional_usd
        self._jitter_min_ms = jitter_min_seconds * 1000
        self._jitter_max_ms = jitter_max_ms
        self._jitter_seed = jitter_seed if jitter_seed is not None else secrets.token_hex(8)
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS halt_state (
                    scope TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS initialized_sources (
                    source_id TEXT PRIMARY KEY,
                    initialized_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ignored_source_legs (
                    source_id TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    initial_sign INTEGER NOT NULL,
                    recorded_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (source_id, coin)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trailing_state (
                    scope TEXT PRIMARY KEY,
                    peak_equity TEXT NOT NULL,
                    last_day TEXT NOT NULL,
                    last_equity TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wave_jitter (
                    wave_key TEXT PRIMARY KEY,
                    first_seen_ms INTEGER NOT NULL,
                    eligible_at_ms INTEGER NOT NULL
                )
                """
            )

    def _apply_source_baselines(
        self,
        positions_by_source: dict[str, dict[str, Decimal]],
        markets: dict[str, MarketSpec],
        current_by_symbol: dict[str, Decimal],
        now_ms: int,
        *,
        persist: bool,
    ) -> tuple[dict[str, Decimal], int]:
        """Exclude pre-existing source legs until closed or flipped across zero.

        ponytail: uses cwsa_heads state directly; misses a close+same-side-reopen that
        completes entirely between two poll ticks (add cwsa_outcomes replay if needed).
        """
        with self._connect() as conn:
            init_rows = conn.execute("SELECT source_id FROM initialized_sources").fetchall()
            initialized = {str(r[0]) for r in init_rows}
            ign_rows = conn.execute(
                "SELECT source_id, coin, initial_sign FROM ignored_source_legs"
            ).fetchall()
            ignored: dict[tuple[str, str], int] = {
                (str(r[0]), str(r[1])): int(r[2]) for r in ign_rows
            }
            migrating_live = (
                not initialized
                and conn.execute("SELECT 1 FROM processed_waves LIMIT 1").fetchone() is not None
            )

        new_sources: list[str] = []
        to_insert: list[tuple[str, str, int, int]] = []
        to_delete: list[tuple[str, str]] = []

        for sid in sorted(positions_by_source):
            pos_map = positions_by_source[sid]
            if sid not in initialized:
                new_sources.append(sid)
                for coin, qty in sorted(pos_map.items()):
                    if qty == _ZERO:
                        continue
                    if migrating_live:
                        spec = _symbol_for_coin(coin, markets)
                        if spec is not None and current_by_symbol.get(spec.symbol, _ZERO) != _ZERO:
                            continue
                    sign = 1 if qty > _ZERO else -1
                    ignored[(sid, coin)] = sign
                    to_insert.append((sid, coin, sign, now_ms))
            else:
                for (ign_sid, coin), init_sign in list(ignored.items()):
                    if ign_sid != sid:
                        continue
                    cur_qty = pos_map.get(coin, _ZERO)
                    cur_sign = 0 if cur_qty == _ZERO else (1 if cur_qty > _ZERO else -1)
                    if cur_qty == _ZERO or cur_sign != init_sign:
                        del ignored[(sid, coin)]
                        to_delete.append((sid, coin))

        if persist and (new_sources or to_insert or to_delete):
            with self._connect() as conn:
                for sid in new_sources:
                    conn.execute(
                        "INSERT OR IGNORE INTO initialized_sources (source_id, initialized_at_ms) "
                        "VALUES (?, ?)",
                        (sid, now_ms),
                    )
                for sid, coin, sign, ts in to_insert:
                    conn.execute(
                        "INSERT OR IGNORE INTO ignored_source_legs "
                        "(source_id, coin, initial_sign, recorded_at_ms) VALUES (?, ?, ?, ?)",
                        (sid, coin, sign, ts),
                    )
                for sid, coin in to_delete:
                    conn.execute(
                        "DELETE FROM ignored_source_legs WHERE source_id = ? AND coin = ?",
                        (sid, coin),
                    )

        net: dict[str, Decimal] = {}
        ignored_count = 0
        for sid, pos_map in positions_by_source.items():
            k = self._member_k[sid]
            for coin, qty in pos_map.items():
                if (sid, coin) in ignored:
                    ignored_count += 1
                    continue
                net[coin] = net.get(coin, _ZERO) + qty * k
        return {c: q for c, q in sorted(net.items()) if q != _ZERO}, ignored_count

    def _day_baseline(self, day_utc: str, equity: Decimal, now_ms: int) -> Decimal:
        with self._connect() as conn:
            if self._day_start_equity_override is None:
                conn.execute("DELETE FROM daily_baselines WHERE day_utc = 'OVERRIDE_APPLIED'")
            else:
                override_str = str(self._day_start_equity_override)
                applied = conn.execute(
                    "SELECT start_equity FROM daily_baselines WHERE day_utc = 'OVERRIDE_APPLIED'"
                ).fetchone()
                if applied is None or str(applied[0]) != override_str:
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_baselines (day_utc, start_equity, recorded_at_ms) "
                        "VALUES (?, ?, ?)",
                        (day_utc, override_str, now_ms),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_baselines (day_utc, start_equity, recorded_at_ms) "
                        "VALUES ('OVERRIDE_APPLIED', ?, ?)",
                        (override_str, now_ms),
                    )
                    return self._day_start_equity_override
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
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO order_intents "
                "(intent_id, wave_key, symbol, quantity, reduce_only, status, broker_id, reason, "
                "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, 'PENDING_SUBMIT', NULL, NULL, ?, ?) "
                "ON CONFLICT(intent_id) DO UPDATE SET "
                "status = 'PENDING_SUBMIT', broker_id = NULL, reason = NULL, updated_at_ms = excluded.updated_at_ms "
                "WHERE order_intents.status NOT IN ('FILLED', 'ACCEPTED', 'PARTIAL')",
                (intent_id, wave_key, symbol, str(qty), int(reduce_only), now_ms, now_ms),
            )
            return cur.rowcount > 0

    def _update_intent_outcome(
        self, intent_id: str, status: str, broker_id: str | None, reason: str | None, now_ms: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE order_intents SET status = ?, broker_id = ?, reason = ?, updated_at_ms = ? "
                "WHERE intent_id = ?",
                (status, broker_id, reason, now_ms, intent_id),
            )

    def _unresolved_intent(self) -> tuple[str, str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT intent_id, symbol, status FROM order_intents "
                "WHERE status IN ('PENDING_SUBMIT', 'UNKNOWN') "
                "ORDER BY updated_at_ms DESC, intent_id ASC LIMIT 1"
            ).fetchone()
            return (str(row[0]), str(row[1]), str(row[2])) if row is not None else None

    def _mark_wave_processed(self, wave_key: str, next_cp_ms: int, now_ms: int, count: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_waves "
                "(wave_key, next_cp_ms, processed_at_ms, intents_count) VALUES (?, ?, ?, ?)",
                (wave_key, next_cp_ms, now_ms, count),
            )

    def _jitter_eligible_at(self, wave_key: str, now_ms: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT eligible_at_ms FROM wave_jitter WHERE wave_key = ?", (wave_key,)
            ).fetchone()
            if row is not None:
                return int(row[0])
            span = self._jitter_max_ms - self._jitter_min_ms + 1
            digest_bytes = hashlib.sha256(
                f"{wave_key}:{self._jitter_seed}".encode("utf-8")
            ).digest()
            delay_ms = self._jitter_min_ms + (int.from_bytes(digest_bytes[:8], "big") % span)
            eligible_at_ms = now_ms + delay_ms
            conn.execute(
                "INSERT OR IGNORE INTO wave_jitter (wave_key, first_seen_ms, eligible_at_ms) "
                "VALUES (?, ?, ?)",
                (wave_key, now_ms, eligible_at_ms),
            )
            return eligible_at_ms

    def _get_halt(self, scope: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT reason FROM halt_state WHERE scope = ?", (scope,)
            ).fetchone()
            return str(row[0]) if row is not None else None

    def _set_halt(self, scope: str, reason: str, now_ms: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO halt_state (scope, reason, updated_at_ms) VALUES (?, ?, ?)",
                (scope, reason, now_ms),
            )

    def _trailing_floor(self, equity: Decimal, day_key: str, now_ms: int) -> tuple[Decimal, str]:
        basis = self._spec.trailing_basis.upper()
        if basis == "STATIC":
            return self._static_floor, "HALT_STATIC_DD"

        with self._connect() as conn:
            row = conn.execute(
                "SELECT peak_equity, last_day, last_equity FROM trailing_state WHERE scope = ?",
                (basis,),
            ).fetchone()
            if row is None:
                peak = max(self._initial, equity) if basis == "INTRADAY_EQUITY" else self._initial
                last_day = day_key
                last_equity = equity
                conn.execute(
                    "INSERT INTO trailing_state "
                    "(scope, peak_equity, last_day, last_equity, updated_at_ms) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (basis, str(peak), last_day, str(last_equity), now_ms),
                )
            else:
                peak = decimal(row[0])
                last_day = str(row[1])
                last_equity = decimal(row[2])
                if basis == "INTRADAY_EQUITY":
                    peak = max(peak, equity)
                elif basis == "EOD_EQUITY":
                    if day_key != last_day:
                        peak = max(peak, last_equity)
                        last_day = day_key
                    last_equity = equity
                else:
                    raise ValueError(f"UNSUPPORTED_TRAILING_BASIS:{basis}")
                conn.execute(
                    "UPDATE trailing_state "
                    "SET peak_equity = ?, last_day = ?, last_equity = ?, updated_at_ms = ? "
                    "WHERE scope = ?",
                    (str(peak), last_day, str(last_equity), now_ms, basis),
                )

        floor = peak - self._trailing_loss
        if self._spec.trailing_locks_at_initial:
            floor = min(floor, self._initial)
        return floor, "HALT_TRAILING_DD"

    def _risk_check(self, snap: BrokerSnapshot, now_ms: int) -> tuple[str, str] | None:
        if self._allow_rest_reconciliation:
            unexpected = [r for r in snap.uncertainty_reasons if r not in _ALLOWED_REST_UNCERTAINTY]
            if unexpected:
                return ("BLOCKED", f"BLOCKED_RECONCILIATION:{','.join(unexpected)}")
            if snap.evidence_schema is not None and not snap.native_inventory_read_complete:
                return ("BLOCKED", "BLOCKED_RECONCILIATION:INVENTORY_INCOMPLETE")
        elif not snap.financial_complete or snap.uncertainty_reasons:
            reasons = ",".join(snap.uncertainty_reasons) or "INCOMPLETE"
            return ("BLOCKED", f"BLOCKED_RECONCILIATION:{reasons}")

        if snap.equity is None:
            return ("BLOCKED", "BLOCKED_EQUITY_MISSING")

        hard = self._get_halt("HARD")
        if hard is not None:
            hard_status = "HALT_STATIC_DD" if self._spec.trailing_basis.upper() == "STATIC" else "HALT_TRAILING_DD"
            return (hard_status, hard)

        if not self._reset_time_hhmm or self._reset_time_hhmm.upper() == "UNKNOWN":
            return ("BLOCKED", "BLOCKED_RESET_TIME_UNKNOWN")
        day_key = _utc_day_key(now_ms, self._reset_time_hhmm)
        floor, dd_status = self._trailing_floor(snap.equity, day_key, now_ms)
        if snap.equity <= floor:
            reason = f"{dd_status}:equity={snap.equity}<={floor}"
            self._set_halt("HARD", reason, now_ms)
            return (dd_status, reason)

        daily = self._get_halt(f"DAY:{day_key}")
        if daily is not None:
            return ("HALT_DAILY_STOP", daily)

        daily_basis = self._spec.daily_basis.upper()
        if daily_basis in ("BALANCE", "MAX_BALANCE_EQUITY"):
            balance = snap.cash if snap.cash is not None else snap.provider_balance
            if balance is None:
                return ("BLOCKED", "BLOCKED_BALANCE_MISSING")
            day_metric = balance if daily_basis == "BALANCE" else max(balance, snap.equity)
        else:
            day_metric = snap.equity

        day_start = self._day_baseline(day_key, day_metric, now_ms)
        if day_start - snap.equity >= self._internal_daily_stop:
            reason = f"HALT_DAILY_STOP:loss={day_start - snap.equity}>={self._internal_daily_stop}"
            self._set_halt(f"DAY:{day_key}", reason, now_ms)
            return ("HALT_DAILY_STOP", reason)
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
        risk = self._risk_check(snap, now_ms)
        if risk is not None and risk[0] == "BLOCKED":
            return {"status": "BLOCKED", "reason": risk[1], "submitted": 0}
        order_halt = self._get_halt("ORDER_UNKNOWN")
        if order_halt is not None:
            return {"status": "HALTED", "reason": order_halt, "submitted": 0}
        unresolved = self._unresolved_intent()
        if unresolved is not None:
            intent_id, sym, st = unresolved
            return {
                "status": "BLOCKED",
                "reason": f"BLOCKED_UNRESOLVED_INTENT:{intent_id}:{sym}:{st}",
                "submitted": 0,
            }

        current_by_symbol: dict[str, Decimal] = {}
        for pos in snap.positions:
            if pos.quantity != _ZERO:
                current_by_symbol[pos.market] = current_by_symbol.get(pos.market, _ZERO) + pos.quantity
        open_order_symbols = {
            o.market
            for o in snap.orders
            if o.status in {"OPEN", "ACCEPTED", "PARTIAL"}
            or (o.status == "UNKNOWN" and str(o.raw_status or "").lower() == "pending")
        }
        skipped: list[dict[str, str]] = []
        target_by_symbol: dict[str, Decimal] = {}
        ignored_legs_count = 0

        if risk is not None:
            # Flatten is just target_by_symbol={} routed through the same delta engine.
            halt_reason = risk[1]
            wave_key = digest(
                {
                    "halt": halt_reason,
                    "pos": {s: str(q) for s, q in sorted(current_by_symbol.items())},
                }
            )
            next_cp_ms = now_ms
        else:
            halt_reason = ""
            wave_key, next_cp_ms, positions_by_source, phases = _load_cwsa_snapshot(
                self._source_db, self._member_k
            )
            non_continuous = [
                (sid, ph) for sid, ph in sorted(phases.items()) if ph != "CONTINUOUS"
            ]
            if non_continuous:
                sid, ph = non_continuous[0]
                return {
                    "status": "BLOCKED",
                    "reason": f"BLOCKED_CWSA_NOT_CONTINUOUS:{sid}:{ph}",
                    "submitted": 0,
                }
            if self._max_source_lag_ms is not None and now_ms - next_cp_ms > self._max_source_lag_ms:
                return {"status": "BLOCKED", "reason": f"BLOCKED_CWSA_STALE:lag_ms={now_ms - next_cp_ms}", "submitted": 0}
            coin_targets, ignored_legs_count = self._apply_source_baselines(
                positions_by_source,
                markets,
                current_by_symbol,
                now_ms,
                persist=execute,
            )
            if self._wave_processed(wave_key):
                return {"status": "ALREADY_PROCESSED", "wave_key": wave_key, "submitted": 0}
            for coin, qty in coin_targets.items():
                spec = _symbol_for_coin(coin, markets)
                if spec is None:
                    skipped.append({"coin": coin, "reason": "UNSUPPORTED_BYBIT_USDT_PERP"})
                    continue
                scaled = qty * Decimal("1000") if spec.symbol.startswith("1000") and not coin.startswith("1000") else qty
                target_by_symbol[spec.symbol] = target_by_symbol.get(spec.symbol, _ZERO) + scaled

        planned: list[dict[str, Any]] = []

        all_symbols = sorted(set(target_by_symbol) | set(current_by_symbol))
        spec_by_symbol = {s.symbol: s for s in markets.values()}
        enforce_hyro_usdt = self._spec.firm_slug in ("hyrotrader", "crypto-fund-trader")
        for sym in all_symbols:
            if enforce_hyro_usdt and (sym == "EUR/USD" or not sym.endswith("/USDT:USDT")):
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
            if (
                risk is None
                and self._min_trade_notional > _ZERO
                and target_by_symbol.get(sym, _ZERO) != _ZERO
            ):
                mark_px = _symbol_mark_price(sym, snap, spec)
                if mark_px is not None and abs(qty_step) * mark_px < self._min_trade_notional:
                    skipped.append({"symbol": sym, "reason": "BELOW_NOTIONAL_DEADBAND"})
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

        if risk is not None and not planned:
            return {"status": "HALTED", "reason": halt_reason, "skipped": skipped, "submitted": 0}

        if not execute:
            return {
                "status": "SHADOW_HALT_FLATTEN" if risk is not None else "SHADOW_DRY_RUN",
                "reason": halt_reason or None,
                "wave_key": wave_key,
                "next_cp_ms": next_cp_ms,
                "planned_count": len(planned),
                "planned": planned,
                "skipped": skipped,
                "ignored_legs_count": ignored_legs_count,
                "submitted": 0,
            }

        if risk is None and self._jitter_max_ms > 0 and planned:
            eligible_at_ms = self._jitter_eligible_at(wave_key, now_ms)
            if now_ms < eligible_at_ms:
                return {
                    "status": "JITTER_DEFERRED",
                    "wave_key": wave_key,
                    "next_cp_ms": next_cp_ms,
                    "eligible_at_ms": eligible_at_ms,
                    "remaining_ms": eligible_at_ms - now_ms,
                    "planned_count": len(planned),
                    "planned": planned,
                    "skipped": skipped,
                    "ignored_legs_count": ignored_legs_count,
                    "submitted": 0,
                }

        results: list[dict[str, Any]] = []
        unknown_halt: str | None = None
        for intent in planned:
            if not self._record_intent_pending(
                intent["intent_id"],
                wave_key,
                intent["market"],
                decimal(intent["quantity"]),
                bool(intent["reduce_only"]),
                now_ms,
            ):
                skipped.append({"symbol": intent["market"], "reason": "INTENT_ALREADY_SUBMITTED"})
                continue
            try:
                outcome = gateway.submit(intent)
            except Exception as exc:
                outcome = Outcome(
                    status="UNKNOWN",
                    broker_id=None,
                    reason=f"SUBMIT_EXCEPTION:{type(exc).__name__}",
                )
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
            if outcome.status == "UNKNOWN":
                unknown_halt = f"HALT_ORDER_UNKNOWN:{intent['intent_id']}:{outcome.reason or 'UNKNOWN'}"
                self._set_halt("ORDER_UNKNOWN", unknown_halt, now_ms)
                break

        if unknown_halt is not None:
            return {
                "status": "HALTED",
                "reason": unknown_halt,
                "wave_key": wave_key,
                "next_cp_ms": next_cp_ms,
                "submitted": len(results),
                "results": results,
                "skipped": skipped,
            }
        if risk is None:
            self._mark_wave_processed(wave_key, next_cp_ms, now_ms, len(results))
            return {
                "status": "EXECUTED",
                "wave_key": wave_key,
                "next_cp_ms": next_cp_ms,
                "submitted": len(results),
                "ignored_legs_count": ignored_legs_count,
                "results": results,
                "skipped": skipped,
            }
        return {
            "status": "HALT_FLATTENED",
            "reason": halt_reason,
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


def _load_markets_json(path: Path) -> dict[str, MarketSpec]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("MARKETS_JSON_INVALID")
    out: dict[str, MarketSpec] = {}
    for coin, row in raw.items():
        if not isinstance(coin, str) or not isinstance(row, dict):
            raise ValueError("MARKETS_JSON_ENTRY_INVALID")
        ref_raw = row.get("reference_price")
        out[coin] = MarketSpec(
            symbol=str(row.get("symbol") or coin),
            min_amount=decimal(row.get("min_amount", "0")),
            amount_step=decimal(row.get("amount_step", "0")),
            reference_price=decimal(ref_raw) if ref_raw is not None else None,
        )
    return out


CwsaExecutor = BybitCwsaExecutor


def _sleep_for_report(report: dict[str, Any], poll_seconds: int) -> None:
    if report.get("status") == "JITTER_DEFERRED":
        remaining_s = max(0.2, float(report.get("remaining_ms", 0)) / 1000.0 + 0.05)
        time.sleep(min(float(poll_seconds), remaining_s))
        return
    time.sleep(poll_seconds)


def _load_k_json(source: Path | str) -> dict[str, Decimal]:
    raw_str = str(source).strip()
    text = raw_str if raw_str.startswith("{") else Path(source).read_text(encoding="utf-8")
    raw = json.loads(text)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("K_JSON_INVALID")
    out: dict[str, Decimal] = {}
    for sid, val in raw.items():
        if not isinstance(sid, str) or not sid.strip() or isinstance(val, bool):
            raise ValueError("K_JSON_ENTRY_INVALID")
        k_dec = decimal(val)
        if not k_dec.is_finite() or k_dec < _ZERO:
            raise ValueError("K_JSON_VALUE_INVALID")
        out[sid.strip()] = k_dec
    return out


def _executor_from_args(
    args: argparse.Namespace, *, allow_rest_reconciliation: bool
) -> BybitCwsaExecutor:
    member_k = _load_k_json(args.k_json) if args.k_json is not None else TRIADA_HYDRO5K_K
    day_start = decimal(args.day_start_equity) if args.day_start_equity is not None else None
    return BybitCwsaExecutor(
        state_db=args.state_db,
        source_db=args.source_db,
        member_k=member_k,
        rulebook_key=args.rulebook_key,
        allow_rest_reconciliation=allow_rest_reconciliation,
        min_trade_notional_usd=decimal(args.min_trade_notional_usd),
        jitter_min_seconds=args.jitter_min_seconds,
        jitter_max_seconds=args.jitter_max_seconds,
        reset_time_hhmm=args.reset_time_hhmm,
        day_start_equity_usd=day_start,
    )


def _run_rest_poll_loop(
    executor: BybitCwsaExecutor,
    gateway: Any,
    markets: dict[str, MarketSpec],
    *,
    execute: bool,
    once: bool,
    poll_seconds: int,
) -> int:
    while True:
        now_ms = int(time.time() * 1000)
        report = executor.step(gateway, markets, now_ms, execute=execute)
        print(json.dumps(report, sort_keys=True), flush=True)
        if once:
            return 0
        _sleep_for_report(report, poll_seconds)


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
    parser.add_argument("--venue", choices=("bybit", "propr", "vanta"), default="bybit")
    parser.add_argument("--rulebook-key", default="hyrotrader:two_step:phase_1")
    parser.add_argument("--env-prefix", default="BYBIT_HYDRO")
    parser.add_argument("--propr-account-id", default=None)
    parser.add_argument("--vanta-account-id", default=None)
    parser.add_argument("--markets-json", type=Path, default=None)
    parser.add_argument("--k-json", default=None)
    parser.add_argument("--reset-time-hhmm", default=None)
    parser.add_argument("--day-start-equity", default=None)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--min-trade-notional-usd", default="30")
    parser.add_argument("--jitter-min-seconds", type=int, default=0)
    parser.add_argument("--jitter-max-seconds", type=int, default=45)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    if args.venue == "propr":
        from connectors.propr.rest import ProprOrderClient, ProprRest

        from .propr_gateway import PROPR_API_URL, ProprGateway

        account_id = (args.propr_account_id or os.environ.get("PROPR_ACCOUNT_ID", "")).strip()
        if not account_id:
            raise ValueError("PROPR_ACCOUNT_ID_REQUIRED")
        if args.markets_json is None:
            raise ValueError("PROPR_MARKETS_JSON_REQUIRED")
        api_key = os.environ.get("PROPR_LIVE_API_KEY") or os.environ["PROPR_API_KEY"]
        endpoint = os.environ.get("PROPR_API_URL", PROPR_API_URL).rstrip("/") or PROPR_API_URL
        markets = _load_markets_json(args.markets_json)
        rest = ProprRest(endpoint, api_key)
        order_client = (
            ProprOrderClient(
                endpoint,
                api_key,
                account_id=account_id,
                confirm_account_id=account_id,
                allow_execution=True,
            )
            if args.execute
            else None
        )
        executor = _executor_from_args(args, allow_rest_reconciliation=True)
        with ProprGateway(rest, account_id=account_id, endpoint=endpoint, order_client=order_client) as gateway:
            return _run_rest_poll_loop(
                executor, gateway, markets, execute=args.execute, once=args.once, poll_seconds=args.poll_seconds
            )

    if args.venue == "vanta":
        from connectors.vanta import VantaClient, VantaConfig, VantaOrderClient

        from .vanta_gateway import VANTA_API_URL, VantaGateway

        account_id = (args.vanta_account_id or os.environ.get("VANTA_ACCOUNT_ID", "")).strip()
        if not account_id:
            raise ValueError("VANTA_ACCOUNT_ID_REQUIRED")
        cfg = VantaConfig(
            account_id=account_id,
            api_key=os.environ["VANTA_API_KEY"],
            api_secret=os.environ["VANTA_API_SECRET"],
        )
        client = VantaClient(cfg)
        vanta_order_client = (
            VantaOrderClient(cfg, confirm_account_id=account_id, allow_execution=True)
            if args.execute
            else None
        )
        executor = _executor_from_args(args, allow_rest_reconciliation=True)
        with VantaGateway(client, account_id=account_id, endpoint=VANTA_API_URL, order_client=vanta_order_client) as gateway:
            markets = _load_markets_json(args.markets_json) if args.markets_json else gateway.load_markets()
            return _run_rest_poll_loop(
                executor, gateway, markets, execute=args.execute, once=args.once, poll_seconds=args.poll_seconds
            )

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
    executor = _executor_from_args(args, allow_rest_reconciliation=False)

    while True:
        reconciler = BybitDemoReconciler(client)
        with BybitDemoGateway(reconciler, expected_demo_uid=uid) as gateway:
            markets = _build_markets(client.exchange)
            while True:
                now_ms = int(time.time() * 1000)
                report = executor.step(gateway, markets, now_ms, execute=args.execute)
                print(json.dumps(report, sort_keys=True), flush=True)
                if args.once:
                    return 0
                if report.get("status") == "BLOCKED" and str(report.get("reason", "")).startswith(
                    "BLOCKED_RECONCILIATION"
                ):
                    time.sleep(args.poll_seconds)
                    break
                _sleep_for_report(report, args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
