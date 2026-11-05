"""Read-only health evidence; process existence is not financial progress."""
from __future__ import annotations

from typing import Any


def assess_health(state: dict[str, Any], *, now_ms: int, source: dict[str, Any] | None = None,
                  market_ms: int | None = None, watchdog_ms: int | None = None) -> dict[str, Any]:
    runtime = state.get("runtime", {})
    reasons = list(state.get("halts", []))
    def age(value: Any) -> int | None:
        return now_ms - value if type(value) is int and 0 <= value <= now_ms else None
    progress_age = age(runtime.get("last_progress_ms"))
    account_age = age(runtime.get("last_snapshot_ms"))
    market_age, watchdog_age = age(market_ms), age(watchdog_ms)
    if progress_age is None or progress_age > 10000:
        reasons.append("FINANCIAL_PROGRESS_STALE")
    if account_age is None or account_age > 5000:
        reasons.append("BROKER_STALE_OR_UNKNOWN")
    if runtime.get("certainty") != "COHERENT":
        reasons.append("RECONCILIATION_UNCERTAIN")
    if not state.get("finance"):
        reasons.append("BROKER_FINANCE_UNKNOWN")
    if market_age is None or market_age > 5000:
        reasons.append("MARKET_STALE_OR_UNKNOWN")
    if watchdog_age is None or watchdog_age > 10000:
        reasons.append("WATCHDOG_UNAVAILABLE")
    source = source or {}
    heartbeat_age = age(source.get("heartbeat_ms"))
    event_age = age(source.get("last_event_ms"))
    book_age = age(source.get("book_known_ms"))
    source_state = "DISCONNECTED"
    if source.get("connected") is True and heartbeat_age is not None and heartbeat_age <= 30000:
        source_state = "QUIET" if event_age is None or event_age > 30000 else "ACTIVE"
    else:
        reasons.append("SOURCE_DISCONNECTED_OR_UNKNOWN")
    if book_age is None or book_age > 600000:
        reasons.append("SOURCE_BOOK_STALE_OR_UNKNOWN")
    return {"schema": "guardian_health_v1", "status": "BLOCKED" if reasons else "OBSERVATION_HEALTHY",
            "reasons": sorted(set(reasons)), "source_state": source_state,
            "ages_ms": {"financial_progress": progress_age, "account": account_age,
                        "market": market_age, "watchdog": watchdog_age,
                        "source_heartbeat": heartbeat_age, "source_book": book_age},
            "flat_confirmed": bool(not reasons and runtime.get("flat_confirmed")),
            "trading_authorized": False}
