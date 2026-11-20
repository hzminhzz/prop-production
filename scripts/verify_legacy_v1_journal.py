#!/usr/bin/env python3
"""Verify a closed Guardian journal copy without opening a writer or recovering it.

Archival verification only. The script opens the caller-supplied journal with
``mode=ro&immutable=1``, recomputes the original hash chain, re-runs the frozen
semantic replay on ``guardian_finance_semantic_v2`` rows, and reports hash-only
evidence for pre-semantic rows. It never writes, migrates, normalises bytes or
authorizes trading, and ``prop_production`` never imports it.

A pre-cutover ``portfolio_release_v1`` release is verified only when the caller
supplies the original signed envelope, the trusted public key and the release
scope explicitly. The signature is checked over the original envelope first and
the payload is validated against the frozen V1 required set, exactly as the
retired live verifier did, so the historical ``release_sha`` is preserved and
never re-derived through a schema projection. Without that evidence the
attestation reports ``release_validation: NOT_EVIDENCED`` and claims nothing
about the release. The historical ``ALLOCATION_OVER_LIVE_NAV`` member mode is
accepted here because it was accepted then; it is a record of what the archive
contains, never a live sizing route.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copytrade_kernel.money import decimal
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from prop_production.evidence import digest, load_json, require_sha, verify
from prop_production.journal_archive import scan_archive

V1_SCHEMA = "portfolio_release_v1"
FIXED_K = "FIXED_K_NATIVE_QUANTITY"
NAV = "ALLOCATION_OVER_LIVE_NAV"
V1_REQUIRED = frozenset(
    {
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
)
V1_MEMBER_FIELDS = frozenset(
    {
        "pod_id",
        "source_identity",
        "identity_known_at_ms",
        "account_scope",
        "allocation",
        "sizing_policy",
        "allowed_markets",
    }
)
RESEARCH_COMMIT = re.compile(r"[a-f0-9]{40}")
MAX_K = "10"


@dataclass(frozen=True)
class ReleaseEvidence:
    envelope: dict[str, Any]
    public_key: Ed25519PublicKey
    authority: str
    account_binding: str
    risk_policy_sha: str
    rulebook_sha: str
    now_ms: int


@dataclass(frozen=True)
class ReleaseAttestation:
    release_sha: str
    generation: int
    authority: str
    sizing_policies: tuple[str, ...]
    envelope_sha: str


def verify_v1_release(evidence: ReleaseEvidence) -> ReleaseAttestation:
    """Frozen pre-cutover V1 validation, signature over the original envelope first."""
    original = verify(evidence.envelope, evidence.public_key, evidence.authority)
    if set(original) != V1_REQUIRED or original["schema"] != V1_SCHEMA:
        raise ValueError("UNSUPPORTED_RELEASE_SCHEMA")
    if type(original["generation"]) is not int or original["generation"] < 1:
        raise ValueError("INVALID_GENERATION")
    if (
        original["account_binding"] != evidence.account_binding
        or original["risk_policy_sha"] != evidence.risk_policy_sha
        or original["rulebook_sha"] != evidence.rulebook_sha
        or original["batch_seconds"] != 300
    ):
        raise ValueError("RELEASE_SCOPE_MISMATCH")
    for field in ("created_at_ms", "expires_at_ms", "selection_known_at_ms"):
        if type(original[field]) is not int or original[field] < 0:
            raise ValueError("INVALID_RELEASE_CLOCK")
    if not (
        original["selection_known_at_ms"]
        <= original["created_at_ms"]
        <= evidence.now_ms
        < original["expires_at_ms"]
    ):
        raise ValueError("EXPIRED_OR_FUTURE_RELEASE")
    if RESEARCH_COMMIT.fullmatch(original["research_commit"]) is None:
        raise ValueError("INVALID_RESEARCH_COMMIT")
    for field in ("inventory_sha", "decision_sha", "definition_sha", "source_sha"):
        require_sha(original[field])
    if original["source_mode"] not in ("SYNTHETIC", "PROSPECTIVE"):
        raise ValueError("INVALID_SOURCE_MODE")
    members = original["members"]
    if not isinstance(members, list) or not members:
        raise ValueError("MISSING_MEMBERS")
    ids: set[str] = set()
    identities: dict[str, tuple[str, ...]] = {}
    policies: set[str] = set()
    for m in members:
        fields = set(V1_MEMBER_FIELDS)
        mode = m.get("sizing_policy")
        if mode == FIXED_K:
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
            or not 0 <= m["identity_known_at_ms"] <= original["selection_known_at_ms"]
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
        if mode == FIXED_K:
            # Research-selected, signed positive k; Guardian still limited
            # stressed pod loss, margin, exposure, and follower drawdown.
            if not 0 < decimal(m["k"]) <= decimal(MAX_K):
                raise ValueError("INVALID_FIXED_K")
        elif mode != NAV:
            raise ValueError("INVALID_SIZING_POLICY")
        policies.add(mode)
    if digest(members) != original["definition_sha"]:
        raise ValueError("DEFINITION_DRIFT")
    expected_source = digest(
        {
            k: original[k]
            for k in (
                "research_commit",
                "decision_sha",
                "inventory_sha",
                "definition_sha",
                "selection_known_at_ms",
            )
        }
    )
    if original["source_sha"] != expected_source:
        raise ValueError("PROVENANCE_DRIFT")
    return ReleaseAttestation(
        release_sha=digest(original),
        generation=original["generation"],
        authority=evidence.authority,
        sizing_policies=tuple(sorted(policies)),
        envelope_sha=digest(evidence.envelope),
    )


def verify_archive(path: Path, evidence: ReleaseEvidence | None = None) -> dict[str, Any]:
    scan = scan_archive(path)
    if scan.legacy_events and scan.semantic_events:
        financial = "LEGACY_CHAIN_ONLY_IN_PART"
    elif scan.legacy_events:
        financial = "LEGACY_CHAIN_ONLY"
    else:
        financial = "VERIFIED"
    result: dict[str, Any] = {
        "integrity": "VERIFIED",
        "sequence": scan.sequence,
        "head": scan.head,
        "legacy_events": scan.legacy_events,
        "semantic_events": scan.semantic_events,
        "financial_semantics": financial,
        "release_validation": "NOT_EVIDENCED",
        "archive_release_shas": list(scan.release_shas),
        "order_route": "NONE",
        "trading_authorized": False,
    }
    if evidence is None:
        return result
    attestation = verify_v1_release(evidence)
    if attestation.release_sha not in scan.release_shas:
        raise ValueError("RELEASE_NOT_BOUND_TO_ARCHIVE")
    result.update(
        release_validation="VERIFIED",
        release_schema=V1_SCHEMA,
        release_sha=attestation.release_sha,
        release_generation=attestation.generation,
        release_authority=attestation.authority,
        release_envelope_sha=attestation.envelope_sha,
        sizing_policies=list(attestation.sizing_policies),
    )
    return result


def load_public_key(path: Path) -> Ed25519PublicKey:
    if path.is_symlink() or not path.is_file():
        raise ValueError("ARCHIVE_PUBLIC_KEY_REQUIRED")
    try:
        raw = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("ARCHIVE_PUBLIC_KEY") from exc
    if len(raw) != 32:
        raise ValueError("ARCHIVE_PUBLIC_KEY")
    return Ed25519PublicKey.from_public_bytes(raw)


def load_envelope(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("ARCHIVE_RELEASE_EVIDENCE_REQUIRED")
    value = load_json(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("ARCHIVE_RELEASE_EVIDENCE")
    return value


def release_evidence(args: argparse.Namespace) -> ReleaseEvidence | None:
    """Explicit archival evidence or nothing: a partial set is refused, never guessed."""
    supplied = {
        "--release": args.release is not None,
        "--public-key": args.public_key is not None,
        "--authority": bool(args.authority),
        "--account-binding": bool(args.account_binding),
        "--risk-policy-sha": bool(args.risk_policy_sha),
        "--rulebook-sha": bool(args.rulebook_sha),
        "--now-ms": args.now_ms is not None,
    }
    missing = sorted(name for name, present in supplied.items() if not present)
    if len(missing) == len(supplied):
        return None
    if missing:
        raise ValueError("ARCHIVE_RELEASE_EVIDENCE_INCOMPLETE:" + ",".join(missing))
    return ReleaseEvidence(
        envelope=load_envelope(args.release),
        public_key=load_public_key(args.public_key),
        authority=args.authority,
        account_binding=args.account_binding,
        risk_policy_sha=require_sha(args.risk_policy_sha),
        rulebook_sha=require_sha(args.rulebook_sha),
        now_ms=args.now_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a closed Guardian journal archive copy, read-only."
    )
    parser.add_argument("journal", type=Path, help="closed, checkpointed archive copy")
    parser.add_argument(
        "--release", type=Path, help="original signed portfolio_release_v1 envelope (JSON)"
    )
    parser.add_argument(
        "--public-key", type=Path, help="trusted release-authority Ed25519 public key (hex)"
    )
    parser.add_argument("--authority", help="release authority named in the envelope")
    parser.add_argument("--account-binding", help="expected signed account binding")
    parser.add_argument("--risk-policy-sha", help="expected signed risk policy sha256")
    parser.add_argument("--rulebook-sha", help="expected signed rulebook sha256")
    parser.add_argument(
        "--now-ms", type=int, help="archival decision instant for the V1 clock window"
    )
    args = parser.parse_args()
    try:
        result = verify_archive(args.journal, release_evidence(args))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(2, f"Archive verification failed ({type(exc).__name__}): {exc}.\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
