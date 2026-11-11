"""Independent application verifier for the pinned native capability wire contract."""

from __future__ import annotations

import re
from typing import Any

FACTS = {"net_order", "reduce_only", "cross_zero"}
FIELDS = {
    "schema",
    "account",
    "endpoint",
    "instrument",
    "observed_at_ms",
    "known_at_ms",
    "valid_until_ms",
    "position_mode",
    "trading_authorized",
} | FACTS


def permits(
    raw: dict[str, Any],
    feature: str,
    *,
    account: str,
    endpoint: str,
    instrument: str,
    now_ms: int,
    synthetic: bool,
) -> bool:
    if feature not in FACTS:
        raise ValueError("CAPABILITY_FEATURE")
    if (
        not isinstance(raw, dict)
        or set(raw) != FIELDS
        or raw["schema"] != "native_execution_capabilities_v1"
        or raw["trading_authorized"] is not False
        or any(
            not isinstance(raw[k], str) or not raw[k].strip()
            for k in ("account", "endpoint", "instrument")
        )
        or any(type(raw[k]) is not int for k in ("observed_at_ms", "known_at_ms", "valid_until_ms"))
        or not 0 <= raw["observed_at_ms"] <= raw["known_at_ms"] <= raw["valid_until_ms"]
        or raw["position_mode"] not in {"UNKNOWN", "ONE_WAY", "HEDGE"}
    ):
        raise ValueError("CAPABILITY_SCHEMA_OR_BINDING")
    for name in FACTS:
        fact = raw[name]
        if not isinstance(fact, dict) or set(fact) != {"status", "evidence_sha", "provenance"}:
            raise ValueError("CAPABILITY_FACT_SCHEMA")
        status = fact["status"]
        if status not in {"UNKNOWN", "UNSUPPORTED", "VERIFIED", "SYNTHETIC"}:
            raise ValueError("CAPABILITY_STATUS")
        if status in {"VERIFIED", "SYNTHETIC"} and (
            not isinstance(fact["evidence_sha"], str)
            or re.fullmatch(r"[a-f0-9]{64}", fact["evidence_sha"]) is None
            or not isinstance(fact["provenance"], str)
            or not fact["provenance"].strip()
        ):
            raise ValueError("CAPABILITY_EVIDENCE_REQUIRED")
        if status == "SYNTHETIC" and (raw["account"], raw["endpoint"]) != (
            "fixture-account",
            "fake://broker",
        ):
            raise ValueError("SYNTHETIC_CAPABILITY_SCOPE")
        if status == "VERIFIED" and not raw["endpoint"].startswith("https://"):
            raise ValueError("NATIVE_CAPABILITY_ENDPOINT")
    return bool(
        type(now_ms) is int
        and type(synthetic) is bool
        and (raw["account"], raw["endpoint"], raw["instrument"]) == (account, endpoint, instrument)
        and raw["known_at_ms"] <= now_ms < raw["valid_until_ms"]
        and raw["position_mode"] == "ONE_WAY"
        and raw[feature]["status"] == ("SYNTHETIC" if synthetic else "VERIFIED")
    )
