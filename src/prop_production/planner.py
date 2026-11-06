"""Causal source receipts and whole-book requests; no broker credentials or authority."""

from __future__ import annotations

import sqlite3
from decimal import localcontext
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import TargetBatchProposal
from .evidence import canonical, decimal, digest, load_json, verify
from .release import VerifiedRelease


def validate_book(
    body: dict[str, Any], member: dict[str, Any], release: dict[str, Any], local_known_ms: int
) -> None:
    fields = {
        "schema",
        "source_identity",
        "source_event_ms",
        "known_at_ms",
        "sequence",
        "scope",
        "scope_complete",
        "book_complete",
        "nav",
        "positions",
        "source_mode",
    }
    if set(body) != fields or body["schema"] != "source_book_v1":
        raise ValueError("SOURCE_SCHEMA")
    if body["source_identity"] != member["source_identity"]:
        raise ValueError("SOURCE_IDENTITY")
    if (
        body["scope_complete"] is not True
        or body["book_complete"] is not True
        or not isinstance(body["scope"], list)
        or sorted(body["scope"]) != sorted(member["account_scope"])
    ):
        raise ValueError("SOURCE_SCOPE_INCOMPLETE")
    if body["source_mode"] != release["source_mode"]:
        raise ValueError("SOURCE_MODE")
    for field in ("source_event_ms", "known_at_ms", "sequence"):
        if type(body[field]) is not int or body[field] < 0:
            raise ValueError("SOURCE_CLOCK")
    if (
        type(local_known_ms) is not int
        or not release["selection_known_at_ms"] <= body["known_at_ms"] <= local_known_ms
        or body["source_event_ms"] > body["known_at_ms"]
    ):
        raise ValueError("SOURCE_CLOCK")
    # Whole-account source NAV is required only for equity-relative fidelity.
    # Fixed-k follows a signed, selected account scope and never substitutes
    # a parent account balance or summed DEX equity for leader NAV.
    if member["sizing_policy"] == "ALLOCATION_OVER_LIVE_NAV":
        if decimal(body["nav"]) <= 0:
            raise ValueError("SOURCE_NAV")
    elif member["sizing_policy"] == "FIXED_K_NATIVE_QUANTITY":
        # A present positive source NAV can be retained as a diagnostic, but
        # it never affects k or becomes a prerequisite for this policy.
        if body["nav"] is not None and decimal(body["nav"]) <= 0:
            raise ValueError("SOURCE_NAV_DIAGNOSTIC_INVALID")
    else:
        raise ValueError("SOURCE_SIZING_POLICY")
    if not isinstance(body["positions"], list) or len(body["positions"]) > 200:
        raise ValueError("SOURCE_BOOK_CAPACITY")
    markets: set[str] = set()
    for leg in body["positions"]:
        if set(leg) != {"market", "quantity", "mark", "mark_known_at_ms"}:
            raise ValueError("SOURCE_LEG_SCHEMA")
        if leg["market"] not in member["allowed_markets"] or leg["market"] in markets:
            raise ValueError("SOURCE_MARKET")
        markets.add(leg["market"])
        if (
            type(leg["mark_known_at_ms"]) is not int
            or not body["source_event_ms"] <= leg["mark_known_at_ms"] <= body["known_at_ms"]
            or decimal(leg["mark"]) <= 0
        ):
            raise ValueError("SOURCE_MARK")
        decimal(leg["quantity"])


def compile_pods(
    receipts: list[dict[str, Any]],
    release: VerifiedRelease,
    source_key: Ed25519PublicKey,
    decision_ms: int,
) -> list[dict[str, Any]]:
    """Guardian re-verifies signatures and derives the requests, independently of Planner output."""
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= 201:
        raise ValueError("SOURCE_RECEIPT_CAPACITY")
    payload = release.payload
    members = {m["source_identity"]: m for m in payload["members"]}
    latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    seen: dict[tuple[str, int], str] = {}
    for receipt in receipts:
        if set(receipt) != {"envelope", "local_known_ms"}:
            raise ValueError("RECEIPT_SCHEMA")
        body = verify(receipt["envelope"], source_key, "source-observer")
        identity = body["source_identity"]
        if identity not in members:
            raise ValueError("SOURCE_IDENTITY")
        validate_book(body, members[identity], payload, receipt["local_known_ms"])
        if receipt["local_known_ms"] > decision_ms:
            raise ValueError("FUTURE_RECEIPT")
        key = (identity, body["sequence"])
        if key in seen and seen[key] != digest(body):
            raise ValueError("SOURCE_SEQUENCE_CONFLICT")
        seen[key] = digest(body)
        if identity not in latest or body["sequence"] > latest[identity][0]["sequence"]:
            latest[identity] = (body, receipt)
    if set(latest) != set(members):
        raise ValueError("SOURCE_BOOK_MISSING")
    pods = []
    for identity, member in members.items():
        body, receipt = latest[identity]
        if decision_ms - body["known_at_ms"] > 600000:
            raise ValueError("SOURCE_STALE")
        with localcontext() as ctx:
            ctx.prec = 50
            if member["sizing_policy"] == "FIXED_K_NATIVE_QUANTITY":
                requested = {
                    leg["market"]: str(
                        decimal(leg["quantity"]) * decimal(leg["mark"]) * decimal(member["k"])
                    )
                    for leg in body["positions"]
                }
            else:
                requested = {
                    leg["market"]: str(
                        decimal(leg["quantity"])
                        * decimal(leg["mark"])
                        / decimal(body["nav"])
                        * decimal(member["allocation"])
                    )
                    for leg in body["positions"]
                }
        pods.append(
            {
                "pod_id": member["pod_id"],
                "requested_notionals": requested,
                "source_receipt_sha": digest(receipt),
                "source_nav": body["nav"],
                "allocation": member["allocation"],
                "sizing_policy": member["sizing_policy"],
            }
        )
    return sorted(pods, key=lambda p: p["pod_id"])


def validate_quotes(quotes: dict[str, Any], now_ms: int) -> None:
    if not isinstance(quotes, dict) or not quotes or len(quotes) > 200:
        raise ValueError("QUOTE_SCHEMA")
    for quote in quotes.values():
        if set(quote) != {"bid", "ask", "known_at_ms"}:
            raise ValueError("QUOTE_SCHEMA")
        if (
            type(quote["known_at_ms"]) is not int
            or not 0 <= now_ms - quote["known_at_ms"] <= 5000
            or not 0 < decimal(quote["bid"]) <= decimal(quote["ask"])
        ):
            raise ValueError("QUOTE_STALE_OR_INVALID")


class Planner:
    def __init__(self, path: Path, release: VerifiedRelease, source_key: Ed25519PublicKey):
        if path.is_symlink():
            raise ValueError("PLANNER_PATH")
        self.release = release
        self.source_key = source_key
        self.con = sqlite3.connect(path, isolation_level=None, timeout=0.1)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.executescript(
            "CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY, identity TEXT NOT NULL,"
            "seq INTEGER NOT NULL,known_ms INTEGER NOT NULL,payload TEXT NOT NULL,sha TEXT NOT NULL,"
            "UNIQUE(identity,seq));"
            "CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1),"
            "release_sha TEXT NOT NULL,anchor INTEGER,last_proposal TEXT,blocked TEXT);"
        )
        if self.con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            self.con.close()
            raise ValueError("PLANNER_STORAGE")
        row = self.con.execute("SELECT release_sha FROM control WHERE id=1").fetchone()
        if row is None:
            self.con.execute(
                "INSERT INTO control VALUES (1,?,NULL,NULL,NULL)", (release.release_sha,)
            )
        elif row[0] != release.release_sha:
            self.con.close()
            raise ValueError("PLANNER_RELEASE_CONFLICT")

    def ingest(self, envelope: dict[str, Any], *, local_known_ms: int) -> None:
        try:
            body = verify(envelope, self.source_key, "source-observer")
            members = {m["source_identity"]: m for m in self.release.payload["members"]}
            if body["source_identity"] not in members:
                raise ValueError("SOURCE_IDENTITY")
            validate_book(
                body, members[body["source_identity"]], self.release.payload, local_known_ms
            )
            latest = self.con.execute(
                "SELECT seq,sha,known_ms FROM receipts WHERE identity=? ORDER BY seq DESC LIMIT 1",
                (body["source_identity"],),
            ).fetchone()
            if latest and body["sequence"] <= latest[0]:
                if body["sequence"] == latest[0] and digest(body) == latest[1]:
                    return
                raise ValueError("SOURCE_SEQUENCE_CONFLICT")
            if latest and local_known_ms < latest[2]:
                raise ValueError("SOURCE_CLOCK_REGRESSION")
            self.con.execute("BEGIN IMMEDIATE")
            self.con.execute(
                "INSERT INTO receipts(identity,seq,known_ms,payload,sha) VALUES (?,?,?,?,?)",
                (
                    body["source_identity"],
                    body["sequence"],
                    local_known_ms,
                    canonical(envelope).decode(),
                    digest(body),
                ),
            )
            # Invalid observations latch a block; another pod cannot silently clear it.
            self.con.execute(
                "UPDATE control SET anchor=COALESCE(anchor,?) WHERE id=1", (local_known_ms,)
            )
            self.con.execute("COMMIT")
        except (ValueError, KeyError, sqlite3.Error):
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            self.con.execute("UPDATE control SET blocked='SOURCE_INTEGRITY' WHERE id=1")
            raise

    def propose(
        self, now_ms: int, broker_epoch: int, quotes: dict[str, Any]
    ) -> TargetBatchProposal | None:
        anchor, last, blocked = self.con.execute(
            "SELECT anchor,last_proposal,blocked FROM control WHERE id=1"
        ).fetchone()
        if blocked:
            return None
        if anchor is None:
            return TargetBatchProposal.from_dict(load_json(last)) if last else None
        if now_ms < anchor + 300000:
            return None
        # Latest complete book per source plus the actual first receipt of this window.
        rows = self.con.execute(
            "SELECT payload,known_ms FROM receipts WHERE id IN "
            "(SELECT MAX(id) FROM receipts WHERE known_ms<=? GROUP BY identity) "
            "OR id=(SELECT MIN(id) FROM receipts WHERE known_ms=?) ORDER BY id",
            (now_ms, anchor),
        ).fetchall()
        receipts = [
            {"envelope": load_json(payload), "local_known_ms": known} for payload, known in rows
        ]
        pods = compile_pods(receipts, self.release, self.source_key, now_ms)
        validate_quotes(quotes, now_ms)
        body: dict[str, Any] = {
            "schema": "target_batch_proposal_v1",
            "release_sha": self.release.release_sha,
            "account_binding": self.release.payload["account_binding"],
            "first_known_ms": anchor,
            "due_ms": anchor + 300000,
            "decision_ms": now_ms,
            "broker_epoch": broker_epoch,
            "pods": pods,
            "source_receipts": receipts,
            "quote_sha": digest(quotes),
        }
        body["proposal_id"] = digest(body)
        proposal = TargetBatchProposal.from_dict(body)
        self.con.execute(
            "UPDATE control SET anchor=NULL,last_proposal=? WHERE id=1",
            (proposal.payload.decode(),),
        )
        return proposal

    def close(self) -> None:
        self.con.close()
