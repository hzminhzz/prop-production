"""Deterministic, public synthetic CWSA evidence for the isolated Judge."""

from hashlib import sha256
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .evidence import canonical, digest, sign

START = 300_000
SOURCE = "fixture-leader"
INSTRUMENT = "hyperliquid/mainnet:perp:default/BTC"
USER = "0x" + "a" * 40
SCOPE = {
    "schema_version": 3,
    "source_id": SOURCE,
    "account_sha": sha256(USER.encode()).hexdigest(),
    "generation": "a" * 64,
    "selection_sha": "b" * 64,
    "dexes": ["default"],
}
POLICY = {
    "name": "cwsa_utc_5m_v3",
    "measurement_sha": "c" * 64,
    "limits": {
        "max_state_age_ms": 2000,
        "max_acquisition_ms": 2000,
        "max_dex_skew_ms": 1000,
        "publication_grace_ms": 5000,
    },
}


def packets(key: Ed25519PrivateKey, quantities: tuple[str, ...]) -> list[dict[str, Any]]:
    epoch = digest(POLICY)
    stream = digest([SOURCE, SCOPE["account_sha"], SCOPE["generation"], epoch])
    before = {
        "phase": "ABSENT",
        "sequence": 0,
        "next_checkpoint_ms": START,
        "economic_sha": None,
        "positions": [],
        "provider_clocks": [],
    }
    registered = dict(before, phase="UNANCHORED", sequence=1)

    def event(
        seq: int,
        prior: str,
        before: dict[str, Any],
        after: dict[str, Any],
        kind: str,
        guard: str,
        at: int,
        proof: str,
        effect: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 3,
            "source_id": SOURCE,
            "scope_generation": SCOPE["generation"],
            "policy_epoch": epoch,
            "sequence": seq,
            "prior_sha": prior,
            "predecessor": before,
            "event": kind,
            "evidence_sha": proof,
            "guard": guard,
            "at_ms": at,
            "actor": "CWSA",
            "successor": after,
            "effect": effect,
        }

    last = event(
        1,
        "0" * 64,
        before,
        registered,
        "REGISTER",
        "REGISTERED_SCOPE",
        START - 1,
        digest([SCOPE, POLICY, START]),
        None,
    )
    result = []
    for index, quantity in enumerate(quantities):
        boundary = START + 300_000 * index
        published = boundary + 10
        original = {
            "time": boundary,
            "assetPositions": []
            if quantity == "0"
            else [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": quantity,
                        "positionValue": str(abs(int(quantity)) * 10000),
                    }
                }
            ],
        }
        originals = {
            digest([None]): canonical([None]).decode(),
            digest(original): canonical(original).decode(),
        }

        def receipt(dex: str, raw_sha: str, req: dict[str, str], count: int) -> dict[str, Any]:
            return {
                "dex": dex,
                "raw_sha": raw_sha,
                "request_sha": digest(req),
                "requested_ms": boundary,
                "received_ms": boundary + 1,
                "state_ms": boundary,
                "position_count": count,
                "request": {"type": req["type"], "user": req.get("user"), "dex": None},
            }

        positions = [] if quantity == "0" else [{"instrument": INSTRUMENT, "quantity": quantity}]
        sample = {
            "schema_version": 3,
            "kind": "COMPLETE",
            "scope": SCOPE,
            "checkpoint_ms": boundary,
            "started_ms": boundary,
            "received_ms": boundary + 1,
            "validated_ms": boundary + 2,
            "catalog": receipt("catalog", digest([None]), {"type": "perpDexs"}, 0),
            "receipts": [
                receipt(
                    "default",
                    digest(original),
                    {"type": "clearinghouseState", "user": USER},
                    len(positions),
                )
            ],
            "positions": positions,
            "limits": POLICY["limits"],
            "reasons": [],
            "financial_authority": "NONE",
        }
        economic = digest(
            [SCOPE["generation"], {p["instrument"]: p["quantity"] for p in positions}]
        )
        before = last["successor"]
        kind = (
            "BASELINE"
            if not index
            else "UNCHANGED"
            if economic == before["economic_sha"]
            else "CHANGED"
        )
        outcome_id = digest(["cwsa_outcome_v3", stream, boundary])
        effect = {
            "schema_version": 3,
            "source_id": SOURCE,
            "scope_generation": SCOPE["generation"],
            "policy_epoch": epoch,
            "checkpoint_ms": boundary,
            "outcome_id": outcome_id,
            "kind": kind,
            "economic_sha": economic,
            "signal_id": digest(["cwsa_target_v3", outcome_id, economic])
            if kind == "CHANGED"
            else None,
            "positions": positions,
            "sample": sample,
            "published_ms": published,
            "reasons": [],
            "financial_authority": "NONE",
        }
        seq = before["sequence"] + 1
        after = dict(
            before,
            phase="CONTINUOUS",
            sequence=seq,
            next_checkpoint_ms=boundary + 300_000,
            economic_sha=economic,
            positions=positions,
            provider_clocks=[["default", boundary]],
        )
        previous = last
        last = event(
            seq,
            digest(previous),
            before,
            after,
            "SAMPLE",
            "COMPLETE_BASELINE" if not index else "COMPLETE_CONTINUOUS",
            published,
            digest(sample),
            effect,
        )
        result.append(
            sign(
                {
                    "schema": "cwsa_checkpoint_packet_v3",
                    "scope": SCOPE,
                    "policy": POLICY,
                    "transitions": [last] if index else [previous, last],
                    "originals": originals,
                    "issued_ms": published,
                    "trading_authorized": False,
                },
                key,
                "cwsa-checkpoint-v3",
            )
        )
    return result
