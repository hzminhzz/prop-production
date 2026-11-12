"""Version-three verification inside the existing source-only receiver boundary."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .release import VerifiedRelease

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import canonical, digest, load_json, require_sha, verify

GRID_MS = 300_000
ZERO = "0" * 64
AUTHORITY = "cwsa-checkpoint-v3"


def fields(value: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("CWSA_FIELDS")
    return value


def integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("CWSA_CLOCK_OR_CURSOR")
    return int(value)


def validate_pins(pins: dict[str, Any]) -> None:
    import re

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


class CheckpointProjection:
    """Private source evidence only. No Guardian journal, permit or broker access."""

    def __init__(
        self,
        path: Path,
        *,
        release_sha: str,
        source_key: Ed25519PublicKey,
        pins: dict[str, Any],
        release: VerifiedRelease | None = None,
    ) -> None:
        fields(pins, {"policy", "scopes"})
        validate_pins(pins)
        self.pins = dict(load_json(canonical(pins)))
        self.key = source_key
        self.release = release
        self.path = path
        self.lock: int | None = None
        self.con: sqlite3.Connection | None = None
        if path.is_symlink() or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
            raise ValueError("CWSA_PRIVATE_RECEIVER_PATH")
        if not pins["scopes"]:
            raise ValueError("CWSA_PINNED_SCOPES_REQUIRED")
        self.lock = os.open(
            path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.con = sqlite3.connect(path, isolation_level=None, timeout=0.1)
            tables = {
                r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and "cwsa_receiver_meta" not in tables:
                raise ValueError("CWSA_LEGACY_DATABASE_MIX")
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=FULL")
            self.con.executescript(
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_meta(id INTEGER PRIMARY KEY, binding TEXT);"
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_heads(source_id TEXT PRIMARY KEY,"
                " seq INTEGER NOT NULL,state TEXT NOT NULL,sha TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_events(source_id TEXT,seq INTEGER,"
                " payload TEXT NOT NULL,sha TEXT NOT NULL,PRIMARY KEY(source_id,seq));"
                "CREATE TABLE IF NOT EXISTS cwsa_receiver_packets(sha TEXT PRIMARY KEY,"
                " payload TEXT NOT NULL,received_ms INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS cwsa_release_transitions(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL);"
            )
            binding = digest(
                {
                    "release_sha": release_sha,
                    "pins": pins,
                    "source_key": sha256(source_key.public_bytes_raw()).hexdigest(),
                }
            )
            row = self.con.execute("SELECT binding FROM cwsa_receiver_meta WHERE id=1").fetchone()
            if row is None:
                self.con.execute("INSERT INTO cwsa_receiver_meta VALUES(1,?)", (binding,))
            else:
                self._verify_release_bindings(row[0], binding)
            os.chmod(path, 0o600)
            if self.con.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("CWSA_RECEIVER_CORRUPT")
            self._verify_heads()
        except BaseException:
            self.close()
            raise

    def _verify_release_bindings(self, genesis: str, expected: str) -> None:
        from .checkpoint_release_binding import replay

        assert self.con is not None
        previous, state, count = genesis, None, 0
        for sequence, raw in self.con.execute(
            "SELECT seq,payload FROM cwsa_release_transitions ORDER BY seq"
        ):
            if self.release is None or self.release.payload["schema"] != "portfolio_release_v2":
                raise ValueError("CWSA_RELEASE_REBIND_CONTEXT")
            record = replay(
                load_json(raw),
                authority_sha=self.release.payload["mandate_authority_sha"],
                source_key=self.key,
            )
            if (
                sequence != count + 1
                or record["predecessor"]["binding_sha"] != previous
                or (state is not None and record["predecessor"] != state)
            ):
                raise ValueError("CWSA_RELEASE_REBIND_CHAIN")
            state = record["successor"]
            previous, count = state["binding_sha"], sequence
        if previous != expected:
            raise ValueError("CWSA_RECEIVER_TRUST_DRIFT")

    def rebind(self, release: VerifiedRelease, key: Ed25519PublicKey, *, now_ms: int) -> None:
        from .checkpoint_release_binding import binding_sha, transition

        assert self.con is not None
        if self.release is None:
            raise ValueError("CWSA_RELEASE_REBIND_CONTEXT")
        if release.release_sha == self.release.release_sha:
            return
        record = transition(self.release, release, key, self.key, at_ms=now_ms).data
        expected = binding_sha(self.release.release_sha, self.pins, self.key)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            genesis = self.con.execute(
                "SELECT binding FROM cwsa_receiver_meta WHERE id=1"
            ).fetchone()[0]
            self._verify_release_bindings(genesis, expected)
            count = self.con.execute("SELECT COUNT(*) FROM cwsa_release_transitions").fetchone()[0]
            self.con.execute(
                "INSERT INTO cwsa_release_transitions VALUES(?,?)",
                (count + 1, canonical(record).decode()),
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.release = release

    def _verify_heads(self) -> None:
        assert self.con is not None
        proofs: dict[tuple[str, int], str] = {}
        for packet_sha, raw, received_ms in self.con.execute("SELECT * FROM cwsa_receiver_packets"):
            if sha256(raw.encode()).hexdigest() != packet_sha:
                raise ValueError("CWSA_RESTART_PACKET_HASH")
            body = self._packet(raw.encode(), now_ms=received_ms)
            for event in body["transitions"]:
                verify_transition(
                    body["scope"],
                    body["policy"],
                    event,
                    body["originals"],
                    issued_ms=body["issued_ms"],
                )
                key = (body["scope"]["source_id"], event["sequence"])
                event_sha = digest(event)
                if key in proofs and proofs[key] != event_sha:
                    raise ValueError("CWSA_RESTART_EQUIVOCATION")
                proofs[key] = event_sha
        seen = set()
        for source, seq, state, head in self.con.execute("SELECT * FROM cwsa_receiver_heads"):
            previous = ZERO
            current = None
            count = 0
            for number, payload, event_sha in self.con.execute(
                "SELECT seq,payload,sha FROM cwsa_receiver_events WHERE source_id=? ORDER BY seq",
                (source,),
            ):
                event = load_json(payload)
                if (
                    number != count + 1
                    or digest(event) != event_sha
                    or proofs.get((source, number)) != event_sha
                    or event["prior_sha"] != previous
                    or (current is not None and event["predecessor"] != current)
                ):
                    raise ValueError("CWSA_RECEIVER_CHAIN")
                seen.add((source, number))
                current, previous, count = event["successor"], event_sha, number
            if count != seq or current != load_json(state) or previous != head:
                raise ValueError("CWSA_RECEIVER_HEAD")
        if seen != set(proofs):
            raise ValueError("CWSA_RESTART_PROOF_COVERAGE")

    def _packet(self, raw: bytes, *, now_ms: int) -> dict[str, Any]:
        integer(now_ms)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("CWSA_PACKET_LIMIT")
        body = verify(load_json(raw), self.key, AUTHORITY)
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
        if body["schema"] != "cwsa_checkpoint_packet_v3" or body["trading_authorized"] is not False:
            raise ValueError("CWSA_PACKET_VERSION")
        scope, policy = body["scope"], body["policy"]
        if scope != self.pins["scopes"].get(scope["source_id"]) or policy != self.pins["policy"]:
            raise ValueError("CWSA_PACKET_SCOPE_OR_POLICY")
        if integer(body["issued_ms"]) > now_ms:
            raise ValueError("CWSA_PACKET_FUTURE")
        transitions = body["transitions"]
        if not isinstance(transitions, list) or not 1 <= len(transitions) <= 256:
            raise ValueError("CWSA_PACKET_TRANSITION_LIMIT")
        if not isinstance(body["originals"], dict):
            raise ValueError("CWSA_ORIGINALS")
        for raw_sha, original in body["originals"].items():
            if not isinstance(original, str) or sha256(original.encode()).hexdigest() != raw_sha:
                raise ValueError("CWSA_ORIGINAL_HASH")
        return body

    def ingest(self, raw: bytes, *, now_ms: int) -> dict[str, Any]:
        assert self.con is not None
        body = self._packet(raw, now_ms=now_ms)
        scope, policy, transitions = body["scope"], body["policy"], body["transitions"]
        packet_sha = sha256(raw).hexdigest()
        if self.con.execute(
            "SELECT 1 FROM cwsa_receiver_packets WHERE sha=?", (packet_sha,)
        ).fetchone():
            return {
                "schema": "cwsa_protected_receiver_v3",
                "state": "DUPLICATE",
                "events": 0,
                "trading_authorized": False,
            }
        source = scope["source_id"]
        row = self.con.execute(
            "SELECT seq,state,sha FROM cwsa_receiver_heads WHERE source_id=?", (source,)
        ).fetchone()
        sequence, current, prior = (
            (0, None, ZERO) if row is None else (row[0], load_json(row[1]), row[2])
        )
        validated = []
        for event in transitions:
            if integer(event["sequence"]) <= sequence:
                seen = self.con.execute(
                    "SELECT sha FROM cwsa_receiver_events WHERE source_id=? AND seq=?",
                    (source, event["sequence"]),
                ).fetchone()
                if seen != (digest(event),):
                    raise ValueError("CWSA_SOURCE_EQUIVOCATION")
                continue
            if event["sequence"] != sequence + 1 or event["prior_sha"] != prior:
                raise ValueError("CWSA_SOURCE_PREDECESSOR")
            if current is not None and event["predecessor"] != current:
                raise ValueError("CWSA_SOURCE_STATE")
            verify_transition(scope, policy, event, body["originals"], issued_ms=body["issued_ms"])
            sequence, current, prior = event["sequence"], event["successor"], digest(event)
            validated.append(event)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            for event in validated:
                self.con.execute(
                    "INSERT INTO cwsa_receiver_events VALUES (?,?,?,?)",
                    (source, event["sequence"], canonical(event).decode(), digest(event)),
                )
            if current is not None:
                self.con.execute(
                    "INSERT INTO cwsa_receiver_heads VALUES (?,?,?,?) ON CONFLICT(source_id)"
                    " DO UPDATE SET seq=excluded.seq,state=excluded.state,sha=excluded.sha",
                    (source, sequence, canonical(current).decode(), prior),
                )
            self.con.execute(
                "INSERT INTO cwsa_receiver_packets VALUES (?,?,?)",
                (packet_sha, raw.decode(), now_ms),
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return {
            "schema": "cwsa_protected_receiver_v3",
            "state": "EVIDENCE_COLLECTED",
            "events": len(validated),
            "trading_authorized": False,
        }

    def status(self) -> dict[str, Any]:
        assert self.con is not None
        heads = {
            r[0]: {"cursor": r[1], "phase": load_json(r[2])["phase"], "head": r[3]}
            for r in self.con.execute("SELECT * FROM cwsa_receiver_heads")
        }
        return {
            "schema": "cwsa_protected_receiver_v3",
            "source_count": len(heads),
            "sources": heads,
            "order_route": "NONE",
            "trading_authorized": False,
        }

    def outcomes(self, *, now_ms: int) -> list[dict[str, Any]]:
        assert self.con is not None
        integer(now_ms)
        result = []
        phases = {
            r[0]: load_json(r[1])["phase"]
            for r in self.con.execute("SELECT source_id,state FROM cwsa_receiver_heads")
        }
        for (payload,) in self.con.execute(
            "SELECT payload FROM cwsa_receiver_events ORDER BY source_id,seq"
        ):
            event = load_json(payload)
            outcome = event["effect"]
            if outcome is None:
                continue
            sample = outcome["sample"]
            deadline = outcome["checkpoint_ms"] + GRID_MS
            if sample is not None and sample["kind"] == "COMPLETE":
                deadline = min(
                    deadline,
                    min(r["state_ms"] for r in sample["receipts"])
                    + self.pins["policy"]["limits"]["max_state_age_ms"],
                )
            result.append(
                {
                    "outcome": outcome,
                    "source_cursor": event["sequence"],
                    "source_chain_sha": digest(event),
                    "deadline_ms": deadline,
                    "opening_candidate": phases[event["source_id"]] == "CONTINUOUS"
                    and outcome["kind"] == "CHANGED"
                    and outcome["published_ms"] <= now_ms < deadline,
                    "trading_authorized": False,
                }
            )
        return result

    def packet_for(self, outcome_id: str) -> dict[str, Any]:
        """Return the original signed envelope proving a committed source outcome."""
        assert self.con is not None
        require_sha(outcome_id)
        for (raw,) in self.con.execute("SELECT payload FROM cwsa_receiver_packets ORDER BY rowid"):
            envelope = load_json(raw)
            if any(
                t["effect"] is not None and t["effect"]["outcome_id"] == outcome_id
                for t in envelope["payload"]["transitions"]
            ):
                return dict(envelope)
        raise ValueError("CWSA_OUTCOME_PROOF_MISSING")

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


def _vector(rows: Any) -> dict[str, Any]:
    import re

    from .evidence import decimal

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
    _vector(value["positions"])
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
    from .evidence import decimal

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
    if _vector(sample["positions"]) != vector:
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
