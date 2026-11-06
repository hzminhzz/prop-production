"""Canonical evidence, exact decimals and domain-separated Ed25519 envelopes."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DOMAIN = b"prop-production/evidence/v1\x00"


def decimal(value: Any) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError("EXACT_DECIMAL_REQUIRED")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("INVALID_DECIMAL") from exc
    if not result.is_finite():
        raise ValueError("NONFINITE_DECIMAL")
    return result


def canonical(value: Any) -> bytes:
    def check(obj: Any) -> None:
        if isinstance(obj, dict):
            if not all(isinstance(k, str) for k in obj):
                raise ValueError("STRING_KEYS_REQUIRED")
            for v in obj.values():
                check(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                check(v)
        elif type(obj) not in (str, int, bool, type(None)):
            raise ValueError("NONCANONICAL_TYPE")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def require_sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("INVALID_SHA256")
    return value


def load_json(data: bytes | str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k in out:
                raise ValueError("DUPLICATE_JSON_KEY")
            out[k] = v
        return out
    value = json.loads(data, object_pairs_hook=pairs)
    canonical(value)
    return value


def sign(payload: dict[str, Any], key: Ed25519PrivateKey, authority: str) -> dict[str, Any]:
    sealed = load_json(canonical(payload))
    message = DOMAIN + authority.encode() + b"\x00" + canonical(sealed)
    return {"payload": sealed, "authority": authority, "sha256": digest(sealed),
            "signature": base64.b64encode(key.sign(message)).decode()}


def verify(envelope: dict[str, Any], key: Ed25519PublicKey, authority: str) -> dict[str, Any]:
    try:
        if set(envelope) != {"payload", "authority", "sha256", "signature"}:
            raise ValueError("INVALID_ENVELOPE")
        if envelope["authority"] != authority or digest(envelope["payload"]) != envelope["sha256"]:
            raise ValueError("EVIDENCE_BINDING_MISMATCH")
        message = DOMAIN + authority.encode() + b"\x00" + canonical(envelope["payload"])
        key.verify(base64.b64decode(envelope["signature"], validate=True), message)
        return dict(load_json(canonical(envelope["payload"])))
    except (InvalidSignature, KeyError, TypeError) as exc:
        raise ValueError("INVALID_SIGNATURE") from exc
