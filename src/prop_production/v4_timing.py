"""Durable, non-financial V4 position timing projection.

This is a shadow *eligibility* clock, not an execution journal. Guardian retains
independent account inventory, reservations, limits, and broker authority. All
inputs are signed by the externally pinned source observer; no fixture key is
used for real observations.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import canonical, decimal, digest, verify
from .planner import validate_book
from .release import VerifiedRelease

ZERO = Decimal("0")
INTENT_TIME_MS = 300_000
STALE_MS = 600_000
KINDS = frozenset(
    {
        "BASELINE",
        "OBSERVATION",
        "UNCHANGED",
        "GAP",
        "SCOPE",
        "STOP",
        "START",
        "TRANSPORT",
        "DIAGNOSTIC",
    }
)


def _quantity_map(body: dict[str, Any]) -> dict[str, Decimal]:
    return {leg["market"]: decimal(leg["quantity"]) for leg in body["positions"]}


def _same_sign(lhs: Decimal, rhs: Decimal) -> bool:
    return lhs * rhs > 0


class V4ShadowProjection:
    """Single-writer transition scheduler with no broker or financial credentials."""

    def __init__(
        self,
        path: Path,
        *,
        release: VerifiedRelease,
        source_key: Ed25519PublicKey,
        cutoff_ms: int,
        initial_cursors: dict[str, int] | None = None,
    ):
        if (
            path.is_symlink()
            or path.parent.is_symlink()
            or not path.parent.is_dir()
            or path.parent.stat().st_mode & 0o077
            or (path.exists() and path.stat().st_mode & 0o077)
        ):
            raise ValueError("V4_PROJECTION_PATH")
        if release.payload["source_mode"] != "PROSPECTIVE":
            raise ValueError("V4_RELEASE_NOT_PROSPECTIVE")
        if type(cutoff_ms) is not int or cutoff_ms < 0:
            raise ValueError("V4_CUTOFF_CLOCK")
        members = release.payload["members"]
        self.members: dict[str, list[dict[str, Any]]] = {}
        for member in members:
            self.members.setdefault(member["source_identity"], []).append(member)
        self.release = release
        self.source_key = source_key
        existed = path.exists()
        self.con = sqlite3.connect(path, timeout=0.1, isolation_level=None)
        if not existed:
            import os

            os.chmod(path, 0o600)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.executescript(
            "CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1),"
            "release_sha TEXT NOT NULL, cutoff_ms INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS streams (identity TEXT PRIMARY KEY,"
            "cursor INTEGER NOT NULL,chain_sha TEXT NOT NULL,epoch TEXT,"
            "continuous INTEGER NOT NULL,known_ms INTEGER NOT NULL,book TEXT);"
            "CREATE TABLE IF NOT EXISTS markets (identity TEXT NOT NULL,market TEXT NOT NULL,"
            "source_qty TEXT NOT NULL,eligible_qty TEXT NOT NULL,"
            "pending_qty TEXT,pending_since_ms INTEGER,pending_epoch TEXT,"
            "PRIMARY KEY(identity,market));"
            "CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY,"
            "identity TEXT NOT NULL,market TEXT NOT NULL,kind TEXT NOT NULL,"
            "previous_qty TEXT NOT NULL,target_qty TEXT NOT NULL,"
            "first_known_ms INTEGER NOT NULL,due_ms INTEGER NOT NULL,"
            "source_receipt_sha TEXT,created_ms INTEGER NOT NULL);"
        )
        if self.con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("V4_PROJECTION_CORRUPT")
        row = self.con.execute("SELECT release_sha,cutoff_ms FROM config WHERE id=1").fetchone()
        if row is None:
            self.con.execute("INSERT INTO config VALUES (1,?,?)", (release.release_sha, cutoff_ms))
            for identity in self.members:
                start = (initial_cursors or {}).get(identity, 0)
                if type(start) is not int or start < 0:
                    raise ValueError("V4_INITIAL_CURSOR_INVALID")
                self.con.execute(
                    "INSERT INTO streams VALUES (?,?,?,?,?,?,?)",
                    (identity, start, "0" * 64, None, 0, cutoff_ms, None),
                )
        elif row != (release.release_sha, cutoff_ms):
            raise ValueError("V4_PROJECTION_RELEASE_OR_CUTOFF_CHANGED")

    def _validate_record(
        self,
        record: dict[str, Any],
        *,
        now_ms: int,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if set(record) != {"proof", "book"}:
            raise ValueError("V4_RECEIPT_SCHEMA")
        proof = verify(record["proof"], self.source_key, "source-observer-v4")
        if proof.get("schema") != "v4_official_event_proof_v1":
            raise ValueError("V4_PROOF_SCHEMA")
        identity = proof.get("source_identity")
        if identity not in self.members:
            raise ValueError("V4_SOURCE_NOT_RELEASE_SELECTED")
        if (
            proof.get("account_sha") != hashlib.sha256(identity.lower().encode()).hexdigest()
            or not isinstance(proof.get("stream_id"), str)
            or re.fullmatch(r"account:[A-Za-z0-9_-]{1,70}", proof["stream_id"]) is None
            # The signed producer binds this alias to the actual account hash.
            # Different frozen portfolios may name the same leader differently.
        ):
            raise ValueError("V4_SOURCE_SCOPE_INVALID")
        if (
            proof.get("trading_authorized") is not False
            or proof.get("kind") not in KINDS
            or type(proof.get("stream_cursor")) is not int
            or proof["stream_cursor"] < 1
            or type(proof.get("first_known_ms")) is not int
            or not 0 <= proof["first_known_ms"] <= now_ms
            or proof["first_known_ms"] < self.cutoff_ms
            or not isinstance(proof.get("stream_epoch"), str)
            or len(proof["stream_epoch"]) != 32
            or not isinstance(proof.get("stream_chain_sha"), str)
            or len(proof["stream_chain_sha"]) != 64
        ):
            raise ValueError("V4_PROOF_CLOCK_OR_CHAIN")
        if (
            proof["kind"] == "OBSERVATION"
            and proof.get("classification") != "OBSERVED_STATE_CHANGE"
        ):
            raise ValueError("V4_NOT_GENUINE_TRANSITION")
        book = None
        if record["book"] is not None:
            book = verify(record["book"], self.source_key, "source-observer")
            if (
                proof.get("source_book_sha") != digest(book)
                or proof.get("full_selected_scope") is not True
                or book["source_identity"] != identity
                or book["source_event_ms"] != proof["source_event_ms"]
                or book["known_at_ms"] != proof["first_known_ms"]
                or book["source_mode"] != "PROSPECTIVE"
            ):
                raise ValueError("V4_SIGNED_BOOK_PROVENANCE")
            for member in self.members[identity]:
                validate_book(book, member, self.release.payload, now_ms)
        elif (
            proof.get("source_book_sha") is not None
            or proof.get("full_selected_scope") is not False
        ):
            raise ValueError("V4_MISSING_REQUIRED_SIGNED_BOOK")
        return proof, book

    @property
    def cutoff_ms(self) -> int:
        return int(self.con.execute("SELECT cutoff_ms FROM config WHERE id=1").fetchone()[0])

    def ingest(self, record: dict[str, Any], *, now_ms: int) -> list[dict[str, Any]]:
        """Atomically record one prospective source event and protective actions."""
        proof, book = self._validate_record(record, now_ms=now_ms)
        identity = proof["source_identity"]
        stream = self.con.execute(
            "SELECT cursor,chain_sha,epoch,continuous,known_ms,book FROM streams WHERE identity=?",
            (identity,),
        ).fetchone()
        if stream is None:
            raise ValueError("V4_SOURCE_UNKNOWN")
        cursor, old_chain, old_epoch, continuous, last_known, old_book = stream
        if proof["stream_cursor"] <= cursor:
            # Exact duplicate is idempotent. Conflicting chains at the same
            # cursor are source equivocation, not a harmless retry.
            if proof["stream_cursor"] == cursor and proof["stream_chain_sha"] != old_chain:
                raise ValueError("V4_SOURCE_CURSOR_EQUIVOCATION")
            return []
        if proof["stream_cursor"] != cursor + 1:
            raise ValueError("V4_SOURCE_CURSOR_GAP")
        if proof["first_known_ms"] < last_known:
            raise ValueError("V4_SOURCE_CLOCK_REGRESSION")
        kind = proof["kind"]
        if kind == "OBSERVATION" and (book is None or proof["continuity"] is not True):
            # An authentic transition with unavailable native marks must be
            # consumed as a risk-fencing observation. Refusing its cursor would
            # permanently stall all subsequent, properly verified source data.
            pass
        if kind == "OBSERVATION" and old_epoch is not None and old_epoch != proof["stream_epoch"]:
            raise ValueError("V4_EPOCH_CHANGED_WITHOUT_BASELINE")
        state = dict(_quantity_map(book)) if book is not None else {}
        actions: list[dict[str, Any]] = []
        self.con.execute("BEGIN IMMEDIATE")
        try:
            if kind in {"GAP", "SCOPE", "STOP", "START"}:
                # No inferred flat and no pending risk through a source gap.
                self.con.execute(
                    "UPDATE markets SET pending_qty=NULL,pending_since_ms=NULL,"
                    "pending_epoch=NULL WHERE identity=?",
                    (identity,),
                )
                continuous = 0
            elif (
                kind == "BASELINE"
                or (kind == "UNCHANGED" and not continuous and book is not None)
                or (
                    kind == "OBSERVATION"
                    and old_epoch is not None
                    and not continuous
                    and book is not None
                    and proof["continuity"] is True
                )
            ):
                # The first verified, fully priced post-deployment frame or
                # recovery from unpriced source state is a diagnostic baseline.
                # No prior T0 is reused and no new-risk action is created.
                # A source GAP itself cannot create an artificial close.
                continuous = int(book is not None and proof["continuity"] is True)
                if book is not None:
                    for market, quantity in state.items():
                        if (
                            self.con.execute(
                                "SELECT 1 FROM markets WHERE identity=? AND market=?",
                                (identity, market),
                            ).fetchone()
                            is None
                        ):
                            self.con.execute(
                                "INSERT INTO markets VALUES (?,?,?,?,?,?,?)",
                                (identity, market, str(quantity), "0", None, None, None),
                            )
            elif kind == "OBSERVATION" and (book is None or proof["continuity"] is not True):
                # Mark/census failure fences pending risk without erasing any
                # outstanding follower inventory or resetting Guardian state.
                self.con.execute(
                    "UPDATE markets SET pending_qty=NULL,pending_since_ms=NULL,"
                    "pending_epoch=NULL WHERE identity=?",
                    (identity,),
                )
                continuous = 0
            elif kind == "OBSERVATION":
                if old_epoch is not None and not continuous:
                    raise ValueError("V4_TRANSITION_NOT_CONTINUOUS")
                continuous = 1
                previous = {
                    market: (decimal(qty), decimal(eligible), pending, since, epoch)
                    for market, qty, eligible, pending, since, epoch in self.con.execute(
                        "SELECT market,source_qty,eligible_qty,pending_qty,pending_since_ms,pending_epoch "
                        "FROM markets WHERE identity=?",
                        (identity,),
                    )
                }
                for market in sorted(set(previous) | set(state)):
                    prior_qty, eligible, pending, since, pending_epoch = previous.get(
                        market, (ZERO, ZERO, None, None, None)
                    )
                    desired = state.get(market, ZERO)
                    new_eligible = eligible
                    new_pending, new_since, new_epoch = pending, since, pending_epoch
                    if desired == ZERO or (
                        _same_sign(eligible, desired) and abs(desired) < abs(eligible)
                    ):
                        new_eligible = desired
                        new_pending, new_since, new_epoch = None, None, None
                    elif eligible != ZERO and not _same_sign(eligible, desired):
                        new_eligible = ZERO
                        new_pending, new_since, new_epoch = None, None, None
                    if new_eligible != eligible:
                        actions.append(
                            self._action(
                                identity,
                                market,
                                "REDUCE_NOW",
                                eligible,
                                new_eligible,
                                proof["first_known_ms"],
                                proof["first_known_ms"],
                                proof["source_book_sha"],
                            )
                        )
                    if desired != new_eligible:
                        if new_pending is None or (
                            new_pending != "0" and not _same_sign(decimal(new_pending), desired)
                        ):
                            new_since = proof["first_known_ms"]
                        new_pending, new_epoch = str(desired), proof["stream_epoch"]
                    else:
                        new_pending, new_since, new_epoch = None, None, None
                    self.con.execute(
                        "INSERT INTO markets VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(identity,market) DO UPDATE SET "
                        "source_qty=excluded.source_qty,eligible_qty=excluded.eligible_qty,"
                        "pending_qty=excluded.pending_qty,"
                        "pending_since_ms=excluded.pending_since_ms,pending_epoch=excluded.pending_epoch",
                        (
                            identity,
                            market,
                            str(desired),
                            str(new_eligible),
                            new_pending,
                            new_since,
                            new_epoch,
                        ),
                    )
            if kind == "UNCHANGED" and book is None:
                # No priced, verified current book -> no fresh risk decisions.
                self.con.execute(
                    "UPDATE markets SET pending_qty=NULL,pending_since_ms=NULL,"
                    "pending_epoch=NULL WHERE identity=?",
                    (identity,),
                )
                continuous = 0
            # TRANSPORT/UNCHANGED advance the signed stream without creating risk.
            self.con.execute(
                "UPDATE streams SET cursor=?,chain_sha=?,epoch=?,continuous=?,known_ms=?,book=? "
                "WHERE identity=?",
                (
                    proof["stream_cursor"],
                    proof["stream_chain_sha"],
                    proof["stream_epoch"],
                    continuous,
                    proof["first_known_ms"],
                    canonical(book).decode() if book is not None else old_book,
                    identity,
                ),
            )
            for action in actions:
                self._insert_action(action, now_ms)
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return actions

    @staticmethod
    def _action(
        identity: str,
        market: str,
        kind: str,
        before: Decimal,
        after: Decimal,
        t0: int,
        due: int,
        sha: str | None,
    ) -> dict[str, Any]:
        payload = {
            "schema": "v4_shadow_eligibility_v1",
            "source_identity": identity,
            "market": market,
            "kind": kind,
            "previous_source_qty": str(before),
            "eligible_source_qty": str(after),
            "first_known_ms": t0,
            "due_ms": due,
            "source_receipt_sha": sha,
            "trading_authorized": False,
        }
        payload["action_id"] = digest(payload)
        return payload

    def _insert_action(self, a: dict[str, Any], now_ms: int) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                a["action_id"],
                a["source_identity"],
                a["market"],
                a["kind"],
                a["previous_source_qty"],
                a["eligible_source_qty"],
                a["first_known_ms"],
                a["due_ms"],
                a["source_receipt_sha"],
                now_ms,
            ),
        )

    def due(self, now_ms: int) -> list[dict[str, Any]]:
        """Exactly one eligibility decision per pending transition; never an order."""
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("V4_DUE_CLOCK")
        self.con.execute("BEGIN IMMEDIATE")
        actions: list[dict[str, Any]] = []
        try:
            rows = self.con.execute(
                "SELECT m.identity,m.market,m.eligible_qty,m.pending_qty,m.pending_since_ms,"
                "m.pending_epoch,s.epoch,s.continuous,s.known_ms,s.book "
                "FROM markets m JOIN streams s ON m.identity=s.identity "
                "WHERE m.pending_qty IS NOT NULL ORDER BY m.identity,m.market"
            ).fetchall()
            for (
                identity,
                market,
                current,
                target,
                since,
                epoch,
                current_epoch,
                continuity,
                known,
                raw,
            ) in rows:
                if since is None or epoch != current_epoch or continuity != 1:
                    continue
                if now_ms < since + INTENT_TIME_MS:
                    continue
                if now_ms < known or now_ms - known > STALE_MS or not raw:
                    # Keep pending but never grant risk on uncertain/stale source.
                    continue
                body = json.loads(raw)
                positions = _quantity_map(body)
                if positions.get(market, ZERO) != decimal(target):
                    raise ValueError("V4_PENDING_SOURCE_DRIFT")
                a = self._action(
                    identity,
                    market,
                    "ENTRY_ELIGIBLE",
                    decimal(current),
                    decimal(target),
                    since,
                    since + INTENT_TIME_MS,
                    digest(body),
                )
                self.con.execute(
                    "UPDATE markets SET eligible_qty=?,pending_qty=NULL,"
                    "pending_since_ms=NULL,pending_epoch=NULL WHERE identity=? AND market=?",
                    (target, identity, market),
                )
                self._insert_action(a, now_ms)
                actions.append(a)
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return actions

    def action(self, action_id: str) -> dict[str, Any]:
        for action in self.actions():
            if action["action_id"] == action_id:
                return action
        raise ValueError("V4_ACTION_UNKNOWN")

    def eligible_pods(self, now_ms: int) -> list[dict[str, Any]]:
        """Risk-neutral signed-source targets; only Guardian may admit follower risk.

        No Planner-owned equity, inventories or broker estimates are consulted.
        A source gap forbids a new aggregate target and never implies flat.
        """
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("V4_ELIGIBILITY_CLOCK")
        result: list[dict[str, Any]] = []
        for identity, source_members in sorted(self.members.items()):
            source_row = self.con.execute(
                "SELECT continuous,known_ms,book FROM streams WHERE identity=?",
                (identity,),
            ).fetchone()
            if source_row is None or source_row[0] != 1:
                raise ValueError("V4_SOURCE_CONTINUITY_UNKNOWN")
            _, known, raw = source_row
            if raw is None or not 0 <= now_ms - known <= STALE_MS:
                raise ValueError("V4_SOURCE_STALE")
            book = json.loads(raw)
            marks = {leg["market"]: decimal(leg["mark"]) for leg in book["positions"]}
            quantities = {
                market: decimal(qty)
                for market, qty in self.con.execute(
                    "SELECT market,eligible_qty FROM markets WHERE identity=?", (identity,)
                )
            }
            for member in source_members:
                if member["sizing_policy"] != "FIXED_K_NATIVE_QUANTITY":
                    raise ValueError("V4_FIXED_K_ONLY")
                exposure = {}
                for market, qty in sorted(quantities.items()):
                    if qty == 0:
                        continue
                    if market not in marks or market not in member["allowed_markets"]:
                        raise ValueError("V4_MARKET_SOURCE_UNKNOWN")
                    exposure[market] = str(qty * marks[market] * decimal(member["k"]))
                result.append(
                    {
                        "pod_id": member["pod_id"],
                        "requested_notionals": exposure,
                        "source_receipt_sha": digest(book),
                        "source_nav": None,
                        "allocation": member["allocation"],
                        "sizing_policy": member["sizing_policy"],
                    }
                )
        return sorted(result, key=lambda pod: pod["pod_id"])

    def actions(self) -> list[dict[str, Any]]:
        """All durable shadow decisions for downstream audited Guardian preview."""
        return [
            {
                "action_id": action_id,
                "source_identity": identity,
                "market": market,
                "kind": kind,
                "previous_source_qty": before,
                "eligible_source_qty": target,
                "first_known_ms": t0,
                "due_ms": due,
                "source_receipt_sha": sha,
                "trading_authorized": False,
            }
            for action_id, identity, market, kind, before, target, t0, due, sha in self.con.execute(
                "SELECT action_id,identity,market,kind,previous_qty,target_qty,"
                "first_known_ms,due_ms,source_receipt_sha FROM actions ORDER BY rowid"
            )
        ]

    def close(self) -> None:
        self.con.close()
