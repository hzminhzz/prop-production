"""Signed release succession changes a consumer binding, never the shared source epoch."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import digest, load_json
from .release import ReleaseVerifier, VerifiedRelease


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


def _release(envelope: dict[str, Any], key: Ed25519PublicKey, at_ms: int) -> VerifiedRelease:
    p = envelope["payload"]
    return ReleaseVerifier(
        key,
        envelope["authority"],
        p["account_binding"],
        p["risk_policy_sha"],
        p["rulebook_sha"],
    ).verify(envelope, now_ms=at_ms)


def transition(
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
    old = _release(old_envelope, key, old.payload["created_at_ms"])
    new = _release(new_envelope, key, at_ms)
    before, after = old.payload, new.payload
    if (
        before["schema"] != "portfolio_release_v2"
        or after["schema"] != "portfolio_release_v2"
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


def replay(
    record: dict[str, Any], *, authority_sha: str, source_key: Ed25519PublicKey
) -> dict[str, Any]:
    proofs = record["proofs"]
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(proofs["authority_public"]))
    if sha256(key.public_bytes_raw()).hexdigest() != authority_sha:
        raise ValueError("CWSA_RELEASE_REBIND_SIGNER")
    old_raw, new_raw = proofs["old_release"], proofs["new_release"]
    old = _release(old_raw, key, old_raw["payload"]["created_at_ms"])
    new = _release(new_raw, key, record["at_ms"])
    expected = transition(old, new, key, source_key, at_ms=record["at_ms"]).data
    if record != expected:
        raise ValueError("CWSA_RELEASE_REBIND_REPLAY")
    return expected
