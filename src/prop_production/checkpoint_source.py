"""Version-three verification inside the existing source-only receiver boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import CHECKPOINT_SCHEMA_V3
from .evidence import decimal, digest, load_json, require_sha, verify

GRID_MS = 300_000
ZERO = "0" * 64
AUTHORITY = CHECKPOINT_SCHEMA_V3


def fields(value: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("CWSA_FIELDS")
    return value


def integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("CWSA_CLOCK_OR_CURSOR")
    return int(value)


def validate_pins(pins: dict[str, Any]) -> None:
    policy = fields(pins["policy"], {"name", "limits", "measurement_sha"})
    if policy["name"] != "cwsa_utc_5m_v3":
        raise ValueError("CWSA_POLICY_VERSION")
    require_sha(policy["measurement_sha"])
    limits = fields(
        policy["limits"],
        {"max_state_age_ms", "max_acquisition_ms", "max_dex_skew_ms", "publication_grace_ms"},
    )
    if (
        any(integer(v) == 0 for k, v in limits.items() if k != "max_dex_skew_ms")
        or integer(limits["max_dex_skew_ms"]) < 0
        or limits["publication_grace_ms"] >= GRID_MS
    ):
        raise ValueError("CWSA_POLICY_LIMIT")
    if not isinstance(pins["scopes"], dict):
        raise ValueError("CWSA_SCOPES")
    for source, scope in pins["scopes"].items():
        fields(
            scope,
            {"schema_version", "source_id", "account_sha", "generation", "selection_sha", "dexes"},
        )
        if scope["schema_version"] != 3 or scope["source_id"] != source or not source:
            raise ValueError("CWSA_SOURCE_ID")
        for key in ("account_sha", "generation", "selection_sha"):
            require_sha(scope[key])
        dexes = scope["dexes"]
        if (
            not isinstance(dexes, list)
            or not 1 <= len(dexes) <= 32
            or dexes != sorted(set(dexes))
            or "default" not in dexes
            or any(
                not isinstance(dex, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dex) is None
                for dex in dexes
            )
        ):
            raise ValueError("CWSA_PINNED_DEX_SCOPE")


def verified_outcome(
    envelope: dict[str, Any],
    key: Ed25519PublicKey,
    scope: dict[str, Any],
    policy: dict[str, Any],
    outcome_id: str,
) -> dict[str, Any]:
    """Reproduce an outcome from its signed packet and original native bodies."""
    body = verify(envelope, key, AUTHORITY)
    fields(
        body,
        {
            "schema",
            "scope",
            "policy",
            "transitions",
            "originals",
            "issued_ms",
            "trading_authorized",
        },
    )
    if (
        body["schema"] != "cwsa_checkpoint_packet_v3"
        or body["scope"] != scope
        or body["policy"] != policy
        or body["trading_authorized"] is not False
    ):
        raise ValueError("CWSA_OUTCOME_SCOPE")
    if not isinstance(body["originals"], dict) or any(
        not isinstance(raw, str) or sha256(raw.encode()).hexdigest() != raw_sha
        for raw_sha, raw in body["originals"].items()
    ):
        raise ValueError("CWSA_OUTCOME_ORIGINAL_HASH")
    selected = []
    for event in body["transitions"]:
        verify_transition(scope, policy, event, body["originals"], issued_ms=body["issued_ms"])
        if event["effect"] is not None and event["effect"]["outcome_id"] == outcome_id:
            selected.append(event["effect"])
    if len(selected) != 1:
        raise ValueError("CWSA_OUTCOME_NOT_UNIQUE")
    return dict(selected[0])


def parse_vector(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise ValueError("CWSA_VECTOR")
    result = {}
    for row in rows:
        fields(row, {"instrument", "quantity"})
        key, value = row["instrument"], row["quantity"]
        if (
            not isinstance(key, str)
            or re.fullmatch(r"hyperliquid/mainnet:perp:[^/\\s:]+/[^/\\s:]+", key) is None
            or not isinstance(value, str)
            or len(value) > 128
        ):
            raise ValueError("CWSA_NATIVE_KEY_OR_QUANTITY")
        quantity = decimal(value)
        if not quantity or abs(int(quantity.as_tuple().exponent)) > 128 or key in result:
            raise ValueError("CWSA_ZERO_DUPLICATE_OR_RANGE")
        result[key] = quantity
    if list(result) != sorted(result):
        raise ValueError("CWSA_VECTOR_ORDER")
    return result


def quantity_text(value: Any) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _state(value: Any) -> dict[str, Any]:
    fields(
        value,
        {"phase", "sequence", "next_checkpoint_ms", "economic_sha", "positions", "provider_clocks"},
    )
    if value["phase"] not in {"ABSENT", "UNANCHORED", "CONTINUOUS", "FENCED"}:
        raise ValueError("CWSA_PHASE")
    integer(value["sequence"])
    if integer(value["next_checkpoint_ms"]) % GRID_MS:
        raise ValueError("CWSA_GRID")
    parse_vector(value["positions"])
    if value["economic_sha"] is not None:
        require_sha(value["economic_sha"])
    clocks = value["provider_clocks"]
    if not isinstance(clocks, list) or any(
        not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
        for pair in clocks
    ):
        raise ValueError("CWSA_STATE_CLOCKS")
    for pair in clocks:
        integer(pair[1])
    return dict(value)


def _native_vector(
    sample: dict[str, Any],
    scope: Mapping[str, Any],
    policy: Mapping[str, Any],
    originals: dict[str, str],
) -> tuple[dict[str, Any], list[list[Any]]]:
    fields(
        sample,
        {
            "schema_version",
            "kind",
            "scope",
            "checkpoint_ms",
            "started_ms",
            "received_ms",
            "validated_ms",
            "catalog",
            "receipts",
            "positions",
            "limits",
            "reasons",
            "financial_authority",
        },
    )
    if (
        sample["schema_version"] != 3
        or sample["kind"] != "COMPLETE"
        or sample["scope"] != scope
        or sample["limits"] != policy["limits"]
        or sample["reasons"]
        or sample["financial_authority"] != "NONE"
    ):
        raise ValueError("CWSA_COMPLETE_SAMPLE")
    limits = policy["limits"]
    boundary = integer(sample["checkpoint_ms"])
    started, received, validated = (
        integer(sample[k]) for k in ("started_ms", "received_ms", "validated_ms")
    )
    if (
        boundary % GRID_MS
        or not boundary <= started <= received <= validated
        or validated >= boundary + limits["publication_grace_ms"]
        or validated - started > limits["max_acquisition_ms"]
    ):
        raise ValueError("CWSA_SAMPLE_CLOCK")
    catalog = sample["catalog"]
    receipts = sample["receipts"]
    if (
        not isinstance(catalog, dict)
        or not isinstance(receipts, list)
        or [r["dex"] for r in receipts] != scope["dexes"]
    ):
        raise ValueError("CWSA_DEX_CENSUS")
    for receipt in [catalog, *receipts]:
        fields(
            receipt,
            {
                "dex",
                "raw_sha",
                "request_sha",
                "requested_ms",
                "received_ms",
                "state_ms",
                "position_count",
                "request",
            },
        )
        require_sha(receipt["request_sha"])
        request_model = fields(receipt["request"], {"type", "user", "dex"})
        request = {k: v for k, v in request_model.items() if v is not None}
        if digest(request) != receipt["request_sha"]:
            raise ValueError("CWSA_REQUEST_HASH")
        if receipt["dex"] == "catalog":
            if request != {"type": "perpDexs"}:
                raise ValueError("CWSA_CATALOG_REQUEST")
        elif (
            request.get("type") != "clearinghouseState"
            or not isinstance(request.get("user"), str)
            or sha256(request["user"].encode()).hexdigest() != scope["account_sha"]
            or request.get("dex", "default") != receipt["dex"]
        ):
            raise ValueError("CWSA_REQUEST_ACCOUNT_OR_DEX")
        if (
            not started
            <= integer(receipt["requested_ms"])
            <= integer(receipt["received_ms"])
            <= validated
        ):
            raise ValueError("CWSA_RECEIPT_CLOCK")
        if receipt["raw_sha"] not in originals:
            raise ValueError("CWSA_ORIGINAL_MISSING")
    if received != max(r["received_ms"] for r in [catalog, *receipts]):
        raise ValueError("CWSA_RECEIPT_HEAD")
    if catalog["request_sha"] != digest({"type": "perpDexs"}):
        raise ValueError("CWSA_CATALOG_REQUEST")
    census = load_json(originals[catalog["raw_sha"]])
    if (
        not isinstance(census, list)
        or not census
        or census[0] is not None
        or sorted(["default", *[entry["name"] for entry in census[1:]]]) != scope["dexes"]
    ):
        raise ValueError("CWSA_NATIVE_CENSUS")
    vector, clocks = {}, []
    for receipt in receipts:
        native = load_json(originals[receipt["raw_sha"]])
        stamp = integer(native["time"])
        if stamp == 0 or stamp != receipt["state_ms"] or stamp > receipt["received_ms"]:
            raise ValueError("CWSA_NATIVE_CLOCK")
        if (
            "user" in native
            and sha256(native["user"].lower().encode()).hexdigest() != scope["account_sha"]
        ):
            raise ValueError("CWSA_NATIVE_ACCOUNT")
        dex = receipt["dex"]
        if "dex" in native and (native["dex"] or "default") != dex:
            raise ValueError("CWSA_NATIVE_DEX")
        rows = native["assetPositions"]
        if not isinstance(rows, list):
            raise ValueError("CWSA_EXPLICIT_POSITIONS")
        seen, count = set(), 0
        for row in rows:
            p = row["position"]
            coin = p["coin"]
            if not isinstance(coin, str) or (dex != "default" and not coin.startswith(dex + ":")):
                raise ValueError("CWSA_NATIVE_NAMESPACE")
            name = coin if dex == "default" else coin[len(dex) + 1 :]
            key = f"hyperliquid/mainnet:perp:{dex}/{name}"
            quantity = decimal(p["szi"])
            if key in seen:
                raise ValueError("CWSA_DUPLICATE_NATIVE_POSITION")
            seen.add(key)
            if quantity:
                count += 1
                vector[key] = quantity
        if count != receipt["position_count"]:
            raise ValueError("CWSA_NATIVE_POSITION_COUNT")
        clocks.append([dex, stamp])
    stamps = [pair[1] for pair in clocks]
    if (
        validated - min(stamps) > limits["max_state_age_ms"]
        or max(stamps) - min(stamps) > limits["max_dex_skew_ms"]
    ):
        raise ValueError("CWSA_NATIVE_STALE_OR_SKEW")
    if parse_vector(sample["positions"]) != vector:
        raise ValueError("CWSA_NATIVE_VECTOR")
    return vector, clocks


def verify_transition(
    scope: Mapping[str, Any],
    policy: Mapping[str, Any],
    event: dict[str, Any],
    originals: dict[str, str],
    *,
    issued_ms: int,
) -> None:
    fields(
        event,
        {
            "schema_version",
            "source_id",
            "scope_generation",
            "policy_epoch",
            "sequence",
            "prior_sha",
            "predecessor",
            "event",
            "evidence_sha",
            "guard",
            "at_ms",
            "actor",
            "successor",
            "effect",
        },
    )
    if (
        event["schema_version"] != 3
        or event["actor"] != "CWSA"
        or event["source_id"] != scope["source_id"]
        or event["scope_generation"] != scope["generation"]
        or event["policy_epoch"] != digest(policy)
        or integer(event["at_ms"]) > issued_ms
    ):
        raise ValueError("CWSA_TRANSITION_BINDING")
    require_sha(event["prior_sha"])
    require_sha(event["evidence_sha"])
    before, after = _state(event["predecessor"]), _state(event["successor"])
    if (
        integer(event["sequence"]) != before["sequence"] + 1
        or after["sequence"] != event["sequence"]
    ):
        raise ValueError("CWSA_TRANSITION_SEQUENCE")
    if event["sequence"] == 1 and event["event"] != "REGISTER":
        raise ValueError("CWSA_GENESIS_REQUIRES_REGISTER")
    expected = dict(before, sequence=event["sequence"])
    effect = event["effect"]
    if event["event"] == "REGISTER":
        if (
            before["phase"] != "ABSENT"
            or before["sequence"] != 0
            or before["positions"]
            or before["economic_sha"] is not None
            or before["provider_clocks"]
            or event["prior_sha"] != ZERO
            or event["at_ms"] > before["next_checkpoint_ms"]
            or effect is not None
            or event["guard"] != "REGISTERED_SCOPE"
            or event["evidence_sha"] != digest([scope, policy, before["next_checkpoint_ms"]])
        ):
            raise ValueError("CWSA_REGISTRATION")
        expected["phase"] = "UNANCHORED"
    elif event["event"] in {"CONFLICT", "LATE"}:
        if (
            effect is not None
            or event["guard"] != {"CONFLICT": "EQUIVOCATION", "LATE": "REORDERED"}[event["event"]]
        ):
            raise ValueError("CWSA_REJECTION")
        expected["phase"] = "FENCED"
    elif event["event"] in {"GAP", "SAMPLE"}:
        fields(
            effect,
            {
                "schema_version",
                "source_id",
                "scope_generation",
                "policy_epoch",
                "checkpoint_ms",
                "outcome_id",
                "kind",
                "economic_sha",
                "signal_id",
                "positions",
                "sample",
                "published_ms",
                "reasons",
                "financial_authority",
            },
        )
        boundary = integer(effect["checkpoint_ms"])
        stream = digest(
            [scope["source_id"], scope["account_sha"], scope["generation"], digest(policy)]
        )
        if (
            boundary != before["next_checkpoint_ms"]
            or effect["schema_version"] != 3
            or effect["outcome_id"] != digest(["cwsa_outcome_v3", stream, boundary])
            or effect["source_id"] != scope["source_id"]
            or effect["scope_generation"] != scope["generation"]
            or effect["policy_epoch"] != digest(policy)
            or effect["published_ms"] != event["at_ms"]
            or effect["financial_authority"] != "NONE"
        ):
            raise ValueError("CWSA_OUTCOME_BINDING")
        expected["next_checkpoint_ms"] = boundary + GRID_MS
        if event["event"] == "GAP":
            if (
                effect["sample"] is not None
                or effect["kind"] != "UNAVAILABLE"
                or event["guard"] != "SCHEDULE_GAP"
                or event["at_ms"] < boundary + GRID_MS
            ):
                raise ValueError("CWSA_GAP")
            expected["phase"] = "FENCED"
        elif effect["kind"] in {"UNAVAILABLE", "QUARANTINE"}:
            sample = effect["sample"]
            if (
                not isinstance(sample, dict)
                or event["evidence_sha"] != digest(sample)
                or not effect["reasons"]
            ):
                raise ValueError("CWSA_FAILURE_EVIDENCE")
            expected["phase"] = "FENCED"
        else:
            sample = effect["sample"]
            vector, clocks = _native_vector(sample, scope, policy, originals)
            if (
                sample["checkpoint_ms"] != boundary
                or event["evidence_sha"] != digest(sample)
                or not sample["validated_ms"]
                <= event["at_ms"]
                < boundary + policy["limits"]["publication_grace_ms"]
                or any(pair[1] < dict(before["provider_clocks"]).get(pair[0], 0) for pair in clocks)
            ):
                raise ValueError("CWSA_CONTINUITY_CLOCK")
            economic = digest(
                [scope["generation"], {k: quantity_text(v) for k, v in vector.items()}]
            )
            kind, guard = {
                "UNANCHORED": ("BASELINE", "COMPLETE_BASELINE"),
                "FENCED": ("RECOVERY_BASELINE", "COMPLETE_RECOVERY"),
                "CONTINUOUS": (
                    "UNCHANGED" if economic == before["economic_sha"] else "CHANGED",
                    "COMPLETE_CONTINUOUS",
                ),
            }[before["phase"]]
            signal_id = (
                digest(["cwsa_target_v3", effect["outcome_id"], economic])
                if kind == "CHANGED"
                else None
            )
            if (
                effect["kind"] != kind
                or event["guard"] != guard
                or effect["economic_sha"] != economic
                or effect["signal_id"] != signal_id
                or effect["positions"] != sample["positions"]
                or effect["reasons"]
            ):
                raise ValueError("CWSA_ECONOMIC_TRANSITION")
            expected.update(
                phase="CONTINUOUS",
                economic_sha=economic,
                positions=sample["positions"],
                provider_clocks=clocks,
            )
        if expected["phase"] == "FENCED" and (
            effect["positions"]
            or effect["signal_id"] is not None
            or effect["economic_sha"] is not None
        ):
            raise ValueError("CWSA_FAILURE_CANNOT_AUTHORIZE")
    else:
        raise ValueError("CWSA_EVENT")
    if after != expected:
        raise ValueError("CWSA_SUCCESSOR")
