"""Signed release verification. Trust and rollback state live outside runtime writes."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import PORTFOLIO_RELEASE_V1, PORTFOLIO_RELEASE_V2, SYNTHETIC_ACCOUNT
from .copy_mandates import verify_release_contract
from .evidence import canonical, decimal, digest, load_json, require_sha, verify


@dataclass(frozen=True)
class VerifiedRelease:
    canonical_payload: bytes
    release_sha: str
    authority: str
    trading_authorized: bool = False
    signed_envelope: bytes | None = None

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
        original = verify(envelope, self.key, self.authority)
        p = original
        if p.get("schema") == PORTFOLIO_RELEASE_V2:
            p = verify_release_contract(p, self.key.public_bytes_raw())
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
        if set(p) != required or p["schema"] != PORTFOLIO_RELEASE_V1:
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
        identities: dict[str, tuple[str, ...]] = {}
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
                or not isinstance(m["source_identity"], str)
            ):
                raise ValueError("AMBIGUOUS_MEMBER")
            ids.add(m["pod_id"])
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
            source_scope = tuple(sorted(m["account_scope"]))
            previous_scope = identities.setdefault(m["source_identity"], source_scope)
            if previous_scope != source_scope:
                raise ValueError("SHARED_SOURCE_SCOPE_CONFLICT")
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
        result = VerifiedRelease(
            canonical(original),
            digest(original),
            self.authority,
            signed_envelope=canonical(envelope),
        )
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
        "schema": PORTFOLIO_RELEASE_V1,
        "generation": 1,
        "account_binding": SYNTHETIC_ACCOUNT,
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


@dataclass(frozen=True)
class ReleaseBinding:
    release_sha: str
    generation: int
    binding_sha: str


@dataclass(frozen=True)
class ReleaseBindingTransition:
    predecessor: ReleaseBinding
    event: Literal["ADOPT_SIGNED_RELEASE"]
    evidence_sha: str
    at_ms: int
    actor: Literal["RELEASE_AUTHORITY"]
    guard: Literal["FORWARD_SAME_CWSA_ACCOUNT_TRUST"]
    successor: ReleaseBinding
    effect: Literal["REBIND_CONSUMER_PRESERVE_SOURCE"]
    proofs: dict[str, Any]

    @property
    def data(self) -> dict[str, Any]:
        return asdict(self)


def binding_sha(release_sha: str, pins: dict[str, Any], source_key: Ed25519PublicKey) -> str:
    return digest(
        {
            "release_sha": release_sha,
            "pins": pins,
            "source_key": sha256(source_key.public_bytes_raw()).hexdigest(),
        }
    )


def _verify_signed_release(
    envelope: dict[str, Any], key: Ed25519PublicKey, at_ms: int
) -> VerifiedRelease:
    p = envelope["payload"]
    return ReleaseVerifier(
        key,
        envelope["authority"],
        p["account_binding"],
        p["risk_policy_sha"],
        p["rulebook_sha"],
    ).verify(envelope, now_ms=at_ms)


def transition_release_binding(
    old: VerifiedRelease,
    new: VerifiedRelease,
    key: Ed25519PublicKey,
    source_key: Ed25519PublicKey,
    *,
    at_ms: int,
) -> ReleaseBindingTransition:
    if old.signed_envelope is None or new.signed_envelope is None:
        raise ValueError("CWSA_RELEASE_ORIGINAL_REQUIRED")
    old_envelope, new_envelope = load_json(old.signed_envelope), load_json(new.signed_envelope)
    old = _verify_signed_release(old_envelope, key, old.payload["created_at_ms"])
    new = _verify_signed_release(new_envelope, key, at_ms)
    before, after = old.payload, new.payload
    if (
        before["schema"] != PORTFOLIO_RELEASE_V2
        or after["schema"] != PORTFOLIO_RELEASE_V2
        or after["generation"] <= before["generation"]
        or after["created_at_ms"] < before["created_at_ms"]
        or after["account_binding"] != before["account_binding"]
        or old.authority != new.authority
        or after["checkpoint_policy"] != before["checkpoint_policy"]
        or after["checkpoint_scopes"] != before["checkpoint_scopes"]
        or after["mandate_authority_sha"] != before["mandate_authority_sha"]
    ):
        raise ValueError("CWSA_RELEASE_FORWARD_SAME_AUTHORITY_REQUIRED")
    pins = {"policy": before["checkpoint_policy"], "scopes": before["checkpoint_scopes"]}
    proofs = {
        "old_release": old_envelope,
        "new_release": new_envelope,
        "authority_public": key.public_bytes_raw().hex(),
    }
    return ReleaseBindingTransition(
        ReleaseBinding(
            old.release_sha, before["generation"], binding_sha(old.release_sha, pins, source_key)
        ),
        "ADOPT_SIGNED_RELEASE",
        digest(proofs),
        at_ms,
        "RELEASE_AUTHORITY",
        "FORWARD_SAME_CWSA_ACCOUNT_TRUST",
        ReleaseBinding(
            new.release_sha, after["generation"], binding_sha(new.release_sha, pins, source_key)
        ),
        "REBIND_CONSUMER_PRESERVE_SOURCE",
        proofs,
    )


def replay_release_binding(
    record: dict[str, Any], *, authority_sha: str, source_key: Ed25519PublicKey
) -> dict[str, Any]:
    proofs = record["proofs"]
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(proofs["authority_public"]))
    if sha256(key.public_bytes_raw()).hexdigest() != authority_sha:
        raise ValueError("CWSA_RELEASE_REBIND_SIGNER")
    old_raw, new_raw = proofs["old_release"], proofs["new_release"]
    old = _verify_signed_release(old_raw, key, old_raw["payload"]["created_at_ms"])
    new = _verify_signed_release(new_raw, key, record["at_ms"])
    expected = transition_release_binding(old, new, key, source_key, at_ms=record["at_ms"]).data
    if record != expected:
        raise ValueError("CWSA_RELEASE_REBIND_REPLAY")
    return expected


def validate_release_binding(previous: dict[str, Any], current: dict[str, Any], kind: str) -> None:
    old, new = previous.get("checkpoint_release"), current.get("checkpoint_release")
    if old is not None and new is None:
        raise ValueError("MANDATE_RELEASE_BINDING_REMOVED")
    if new is None:
        return
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(new["authority_public"]))
    body = verify(new["envelope"], key, new["envelope"]["authority"])
    if (
        body["schema"] != PORTFOLIO_RELEASE_V2
        or body["account_binding"] != current["account_binding"]
        or digest(body) != current.get("active_release_sha")
        or body["mandate_authority_sha"] != sha256(key.public_bytes_raw()).hexdigest()
    ):
        raise ValueError("MANDATE_RELEASE_BINDING_PROOF")
    if new != old:
        if kind != "BIND_RELEASE_DISARMED" or current["runtime"]["permit"] is not None:
            raise ValueError("MANDATE_RELEASE_BINDING_TRANSITION")
        if old is not None:
            before = verify(old["envelope"], key, old["envelope"]["authority"])
            if (
                old["authority_public"] != new["authority_public"]
                or body["generation"] <= before["generation"]
                or body["checkpoint_policy"] != before["checkpoint_policy"]
                or body["checkpoint_scopes"] != before["checkpoint_scopes"]
                or "RELEASE_CHANGED_REQUIRES_REVIEW" not in current["halts"]
            ):
                raise ValueError("MANDATE_RELEASE_FORWARD_TRUST")
        if any(
            previous.get(k) != current.get(k)
            for k in (
                "finance",
                "positions",
                "pod_positions",
                "fills",
                "intents",
                "reservations",
                "pod_fees_paid",
                "pod_realized",
                "pod_loss_used",
                "checkpoint_basis",
            )
        ):
            raise ValueError("MANDATE_RELEASE_MUTATED_OBLIGATIONS")
