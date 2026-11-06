"""Signed release verification. Trust and rollback state live outside runtime writes."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import canonical, decimal, digest, load_json, require_sha, verify


@dataclass(frozen=True)
class VerifiedRelease:
    canonical_payload: bytes
    release_sha: str
    authority: str
    trading_authorized: bool = False

    @property
    def payload(self) -> dict[str, Any]:
        return dict(load_json(self.canonical_payload))


class ReleaseFloor:
    """Provision once by release authority; runtime uses read-only verification."""

    def __init__(self, path: Path, *, initialize: bool = False):
        if path.is_symlink() or not path.parent.is_dir():
            raise ValueError("UNSAFE_TRUST_PATH")
        if not initialize and not path.is_file():
            raise ValueError("MISSING_ROLLBACK_AUTHORITY")
        self.path = path
        self.con = (
            sqlite3.connect(path, timeout=0.1, isolation_level=None)
            if initialize
            else sqlite3.connect(
                f"file:{path.resolve()}?mode=ro", uri=True, timeout=0.1, isolation_level=None
            )
        )
        if not initialize:
            self.con.execute("PRAGMA query_only=ON")
        self.con.execute("PRAGMA synchronous=FULL")
        if initialize:
            self.con.execute(
                "CREATE TABLE IF NOT EXISTS releases "
                "(account TEXT PRIMARY KEY, generation INTEGER, sha TEXT)"
            )
            # DELETE journal mode: runtime mode=ro opens need zero write access
            # (WAL read-only attaches still create the -shm sidecar).
            self.con.execute("PRAGMA journal_mode=DELETE")
            os.chmod(path, 0o600)
        if self.con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("TRUST_STORE_CORRUPT")

    def check(self, release: VerifiedRelease) -> None:
        p = release.payload
        row = self.con.execute(
            "SELECT generation,sha FROM releases WHERE account=?", (p["account_binding"],)
        ).fetchone()
        if row and (
            p["generation"] < row[0]
            or (p["generation"] == row[0] and release.release_sha != row[1])
        ):
            raise ValueError("RELEASE_ROLLBACK_OR_EQUIVOCATION")

    def require_active(self, release: VerifiedRelease) -> None:
        self.check(release)
        row = self.con.execute(
            "SELECT generation,sha FROM releases WHERE account=?",
            (release.payload["account_binding"],),
        ).fetchone()
        if row != (release.payload["generation"], release.release_sha):
            raise ValueError("RELEASE_NOT_ACTIVATED")

    def accept(self, release: VerifiedRelease) -> None:
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self.check(release)
            p = release.payload
            self.con.execute(
                "INSERT INTO releases VALUES (?,?,?) ON CONFLICT(account) "
                "DO UPDATE SET generation=excluded.generation,sha=excluded.sha",
                (p["account_binding"], p["generation"], release.release_sha),
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> ReleaseFloor:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class ReleaseVerifier:
    def __init__(
        self,
        key: Ed25519PublicKey,
        authority: str,
        account_binding: str,
        risk_policy_sha: str,
        rulebook_sha: str,
    ):
        self.key = key
        self.authority = authority
        self.account_binding = account_binding
        self.risk_policy_sha = require_sha(risk_policy_sha)
        self.rulebook_sha = require_sha(rulebook_sha)

    def verify(
        self, envelope: dict[str, Any], *, now_ms: int, floor: ReleaseFloor | None = None
    ) -> VerifiedRelease:
        p = verify(envelope, self.key, self.authority)
        required = {
            "schema",
            "generation",
            "account_binding",
            "created_at_ms",
            "expires_at_ms",
            "selection_known_at_ms",
            "members",
            "definition_sha",
            "inventory_sha",
            "decision_sha",
            "source_sha",
            "research_commit",
            "risk_policy_sha",
            "rulebook_sha",
            "batch_seconds",
            "source_mode",
        }
        if set(p) != required or p["schema"] != "portfolio_release_v1":
            raise ValueError("UNSUPPORTED_RELEASE_SCHEMA")
        if type(p["generation"]) is not int or p["generation"] < 1:
            raise ValueError("INVALID_GENERATION")
        if (
            p["account_binding"] != self.account_binding
            or p["risk_policy_sha"] != self.risk_policy_sha
            or p["rulebook_sha"] != self.rulebook_sha
            or p["batch_seconds"] != 300
        ):
            raise ValueError("RELEASE_SCOPE_MISMATCH")
        for field in ("created_at_ms", "expires_at_ms", "selection_known_at_ms"):
            if type(p[field]) is not int or p[field] < 0:
                raise ValueError("INVALID_RELEASE_CLOCK")
        if not p["selection_known_at_ms"] <= p["created_at_ms"] <= now_ms < p["expires_at_ms"]:
            raise ValueError("EXPIRED_OR_FUTURE_RELEASE")
        if not re.fullmatch(r"[a-f0-9]{40}", p["research_commit"]):
            raise ValueError("INVALID_RESEARCH_COMMIT")
        for field in ("inventory_sha", "decision_sha", "definition_sha", "source_sha"):
            require_sha(p[field])
        if p["source_mode"] not in ("SYNTHETIC", "PROSPECTIVE"):
            raise ValueError("INVALID_SOURCE_MODE")
        members = p["members"]
        if not isinstance(members, list) or not members:
            raise ValueError("MISSING_MEMBERS")
        ids: set[str] = set()
        identities: set[str] = set()
        for m in members:
            fields = {
                "pod_id",
                "source_identity",
                "identity_known_at_ms",
                "account_scope",
                "allocation",
                "sizing_policy",
                "allowed_markets",
            }
            mode = m.get("sizing_policy")
            if mode == "FIXED_K_NATIVE_QUANTITY":
                fields.add("k")
            if set(m) != fields:
                raise ValueError("INCOMPLETE_MEMBER")
            if (
                not m["pod_id"]
                or m["pod_id"] in ids
                or not m["source_identity"]
                or m["source_identity"] in identities
            ):
                raise ValueError("AMBIGUOUS_MEMBER")
            ids.add(m["pod_id"])
            identities.add(m["source_identity"])
            if (
                type(m["identity_known_at_ms"]) is not int
                or not 0 <= m["identity_known_at_ms"] <= p["selection_known_at_ms"]
            ):
                raise ValueError("POST_SEAL_IDENTITY")
            if (
                not m["account_scope"]
                or len(set(m["account_scope"])) != len(m["account_scope"])
                or m["source_identity"] not in m["account_scope"]
                or not m["allowed_markets"]
            ):
                raise ValueError("INCOMPLETE_ACCOUNT_SCOPE")
            if decimal(m["allocation"]) <= 0:
                raise ValueError("INVALID_SIZING_POLICY")
            if mode == "FIXED_K_NATIVE_QUANTITY":
                # Research-selected, signed positive k; Guardian still limits
                # stressed pod loss, margin, exposure, and follower drawdown.
                if not 0 < decimal(m["k"]) <= decimal("10"):
                    raise ValueError("INVALID_FIXED_K")
            elif mode != "ALLOCATION_OVER_LIVE_NAV":
                raise ValueError("INVALID_SIZING_POLICY")
        if digest(members) != p["definition_sha"]:
            raise ValueError("DEFINITION_DRIFT")
        expected_source = digest(
            {
                k: p[k]
                for k in (
                    "research_commit",
                    "decision_sha",
                    "inventory_sha",
                    "definition_sha",
                    "selection_known_at_ms",
                )
            }
        )
        if p["source_sha"] != expected_source:
            raise ValueError("PROVENANCE_DRIFT")
        result = VerifiedRelease(canonical(p), digest(p), self.authority)
        if floor is not None:
            floor.check(result)
        return result


def fixture_release() -> dict[str, Any]:
    """Invented, unsigned test data. A fixture never establishes real source evidence."""
    members = [
        {
            "pod_id": "pod-a",
            "source_identity": "fixture-leader",
            "identity_known_at_ms": 1,
            "account_scope": ["fixture-leader"],
            "allocation": "2000",
            "sizing_policy": "ALLOCATION_OVER_LIVE_NAV",
            "allowed_markets": ["BTC", "ETH"],
        }
    ]
    p: dict[str, Any] = {
        "schema": "portfolio_release_v1",
        "generation": 1,
        "account_binding": "fixture-account",
        "created_at_ms": 10,
        "expires_at_ms": 1000000,
        "selection_known_at_ms": 5,
        "members": members,
        "definition_sha": digest(members),
        "inventory_sha": digest("synthetic-inventory"),
        "decision_sha": digest("synthetic-decision"),
        "research_commit": "a" * 40,
        "risk_policy_sha": digest("synthetic-risk"),
        "rulebook_sha": digest("synthetic-rules"),
        "batch_seconds": 300,
        "source_mode": "SYNTHETIC",
    }
    p["source_sha"] = digest(
        {
            k: p[k]
            for k in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    return p
