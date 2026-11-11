"""Pure release preflight. A signed candidate grants observation, never activation.

Trust roots, account/risk scope, code pins and the signer floor are supplied by
the existing release owner. This module cannot provision keys, change floors,
open a broker route, or write a financial/source journal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import canonical, digest, verify
from .release import ReleaseFloor, ReleaseVerifier, VerifiedRelease


@dataclass(frozen=True)
class CandidateTransition:
    predecessor: Literal["UNVERIFIED"]
    event: Literal["VERIFY_SIGNED_CANDIDATE"]
    evidence_sha: str
    at_ms: int
    guard: Literal["EXACT_PINS_TRUST_FUTURE_BOUNDARY"]
    successor: Literal["SHADOW_VERIFIED"]
    effect: Literal["OBSERVATION_ONLY"]


@dataclass(frozen=True)
class VerifiedCandidate:
    original: bytes
    release: VerifiedRelease
    transition: CandidateTransition


def verify_candidate(
    envelope: dict[str, Any],
    *,
    verifier: ReleaseVerifier,
    source_key: Ed25519PublicKey,
    floor: ReleaseFloor,
    expected_compatibility: dict[str, Any],
    application_commit: str,
    now_ms: int,
) -> VerifiedCandidate:
    body = verify(envelope, verifier.key, "cwsa-shadow-candidate-v1")
    fields = {
        "schema",
        "release",
        "compatibility",
        "application_commit",
        "trust_roots",
        "created_at_ms",
        "effective_from_ms",
        "mode",
        "trading_authorized",
    }
    if (
        set(body) != fields
        or body["schema"] != "cwsa_shadow_candidate_v1"
        or body["mode"] != "SHADOW_ONLY"
        or body["trading_authorized"] is not False
        or re.fullmatch(r"[0-9a-f]{40}", application_commit) is None
        or body["application_commit"] != application_commit
        or body["compatibility"] != expected_compatibility
    ):
        raise ValueError("SHADOW_CANDIDATE_EXACT_PINS")
    contracts = expected_compatibility["contracts"]
    if (
        contracts["execution_mode"] != "SHADOW"
        or contracts["trading_authorized"] is not False
        or contracts.get("checkpoint_source") != "cwsa_checkpoint_packet_v3"
        or contracts.get("copy_mandate") != "copy_mandate_v1"
        or "portfolio_release_v2" not in contracts.get("portfolio_release_versions", [])
        or 3 not in contracts["source_wire_versions"]
    ):
        raise ValueError("SHADOW_CANDIDATE_CONTRACT")
    if (
        type(now_ms) is not int
        or type(body["created_at_ms"]) is not int
        or type(body["effective_from_ms"]) is not int
        or not 0 <= body["created_at_ms"] <= now_ms < body["effective_from_ms"]
        or body["effective_from_ms"] % 300_000
    ):
        raise ValueError("SHADOW_CANDIDATE_FUTURE_BOUNDARY")
    release = verifier.verify(body["release"], now_ms=now_ms, floor=floor)
    payload = release.payload
    key_sha = sha256(verifier.key.public_bytes_raw()).hexdigest()
    if (
        body["trust_roots"]
        != {
            "release": key_sha,
            "mandate": key_sha,
            "source": sha256(source_key.public_bytes_raw()).hexdigest(),
        }
        or payload["schema"] != "portfolio_release_v2"
        or payload["mandate_authority_sha"] != key_sha
        or payload["created_at_ms"] > body["created_at_ms"]
        or payload["expires_at_ms"] <= body["effective_from_ms"]
        or payload["research_commit"]
        != expected_compatibility["repositories"]["copytrade-pm"]["commit"]
        or any(m["effective_from_ms"] != body["effective_from_ms"] for m in payload["mandates"])
    ):
        raise ValueError("SHADOW_CANDIDATE_TRUST_SCOPE_OR_RELEASE")
    return VerifiedCandidate(
        canonical(envelope),
        release,
        CandidateTransition(
            "UNVERIFIED",
            "VERIFY_SIGNED_CANDIDATE",
            digest(body),
            now_ms,
            "EXACT_PINS_TRUST_FUTURE_BOUNDARY",
            "SHADOW_VERIFIED",
            "OBSERVATION_ONLY",
        ),
    )
