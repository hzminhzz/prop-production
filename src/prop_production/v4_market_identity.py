"""Native market identities and independent follower quote proof.

The Propr public display catalog is an instrument *allowlist only*. It is NOT
provider order permission, executable depth, a fill, or account reconciliation.
An external, separately pinned quote publisher must attest actual provider
bid/ask with its own key. No source/Vike mark may be substituted as a quote.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence import decimal, load_json, require_sha, verify
from .release import VerifiedRelease

NATIVE = re.compile(r"hyperliquid/mainnet:perp:(default|[a-z0-9_-]{1,40})/([A-Za-z0-9._-]{1,64})\Z")
MAX_MARKETS = 250


def load_display_catalog(path: Path, expected_sha: str) -> frozenset[str]:
    """Read exact dated catalog bytes; never infer trading permission."""
    require_sha(expected_sha)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 250_000:
        raise ValueError("V4_MARKET_CATALOG_STORAGE")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("V4_MARKET_CATALOG_PIN_DRIFT")
    catalog = load_json(raw)
    if (
        catalog.get("schema") != "paper_public_physical_sockets_v1"
        or catalog.get("propr_order_ids_attested") is not False
        or catalog.get("paper_admitted") is not False
        or type(catalog.get("native_market_count")) is not int
        or not 1 <= catalog["native_market_count"] <= MAX_MARKETS
    ):
        raise ValueError("V4_MARKET_CATALOG_AUTHORITY")
    groups = catalog.get("physical_sockets")
    if not isinstance(groups, list) or not groups:
        raise ValueError("V4_MARKET_CATALOG_GROUPS")
    markets: list[str] = []
    for group in groups:
        if (
            not isinstance(group, dict)
            or set(group) != {"name", "instruments"}
            or not isinstance(group["instruments"], list)
            or not group["instruments"]
        ):
            raise ValueError("V4_MARKET_CATALOG_GROUPS")
        markets.extend(group["instruments"])
    if (
        len(markets) != catalog["native_market_count"]
        or len(set(markets)) != len(markets)
        or any(
            not isinstance(market, str) or NATIVE.fullmatch(market) is None for market in markets
        )
    ):
        raise ValueError("V4_MARKET_IDENTITY_AMBIGUOUS")
    return frozenset(markets)


def validate_release_markets(
    release: VerifiedRelease,
    catalog: frozenset[str],
) -> None:
    """One-to-one exact native IDs; no base ticker aliasing or cross-DEX collapse."""
    if not catalog or len(catalog) > MAX_MARKETS:
        raise ValueError("V4_MARKET_CATALOG_EMPTY")
    for member in release.payload["members"]:
        markets = member["allowed_markets"]
        if (
            not isinstance(markets, list)
            or not markets
            or len(set(markets)) != len(markets)
            or any(not isinstance(market, str) or market not in catalog for market in markets)
        ):
            raise ValueError("V4_RELEASE_MARKET_NOT_IN_DISPLAY_SCOPE")


def validate_independent_quotes(
    envelope: dict[str, Any],
    *,
    quote_key: Ed25519PublicKey,
    source_key: Ed25519PublicKey,
    release: VerifiedRelease,
    catalog: frozenset[str],
    now_ms: int,
) -> dict[str, dict[str, str | int]]:
    """Verify separately signed provider bid/ask; never manufacture market data.

    A key proves signer provenance, not provider economic truth. Deployment
    additionally needs an independently assessed provider adapter and signer.
    """
    if quote_key.public_bytes_raw() == source_key.public_bytes_raw():
        raise ValueError("V4_QUOTE_SIGNER_NOT_INDEPENDENT")
    validate_release_markets(release, catalog)
    body = verify(envelope, quote_key, "follower-independent-quote")
    if (
        not isinstance(body, dict)
        or set(body)
        != {
            "schema",
            "account_binding",
            "release_sha",
            "known_at_ms",
            "provider_endpoint",
            "provider_scope",
            "quotes",
            "mutation_authorized",
            "source_kind",
        }
        or body["schema"] != "follower_quote_book_v1"
        or body["account_binding"] != release.payload["account_binding"]
        or body["release_sha"] != release.release_sha
        or body["mutation_authorized"] is not False
        or body["source_kind"] != "INDEPENDENT_FOLLOWER_VENUE_BID_ASK"
        or not isinstance(body["provider_endpoint"], str)
        or not body["provider_endpoint"].startswith("https://")
        or not isinstance(body["provider_scope"], str)
        or not body["provider_scope"]
        or type(body["known_at_ms"]) is not int
        or not 0 <= now_ms - body["known_at_ms"] <= 5000
        or not isinstance(body["quotes"], dict)
        or not 1 <= len(body["quotes"]) <= MAX_MARKETS
    ):
        raise ValueError("V4_INDEPENDENT_QUOTE_PROVENANCE")
    needed = {m for member in release.payload["members"] for m in member["allowed_markets"]}
    if not needed.issubset(body["quotes"]):
        raise ValueError("V4_INDEPENDENT_QUOTE_COVERAGE")
    quotes: dict[str, dict[str, str | int]] = {}
    for market, quote in body["quotes"].items():
        if market not in catalog or NATIVE.fullmatch(market) is None:
            raise ValueError("V4_QUOTE_CROSS_DEX_OR_UNKNOWN_MARKET")
        if (
            not isinstance(quote, dict)
            or set(quote) != {"bid", "ask", "known_at_ms", "venue_native_id"}
            or type(quote["known_at_ms"]) is not int
            or not 0 <= now_ms - quote["known_at_ms"] <= 5000
            or quote["known_at_ms"] > body["known_at_ms"]
            or quote["venue_native_id"] != market
        ):
            raise ValueError("V4_INDEPENDENT_QUOTE_IDENTITY_OR_CLOCK")
        bid, ask = decimal(quote["bid"]), decimal(quote["ask"])
        if bid <= 0 or ask <= bid or (ask - bid) / bid > Decimal("0.01"):
            raise ValueError("V4_INDEPENDENT_QUOTE_SPREAD")
        quotes[market] = {
            "bid": str(bid),
            "ask": str(ask),
            "known_at_ms": quote["known_at_ms"],
        }
    return quotes
