"""Native market/DEX allowlisting and independent signed quote rejection cases."""

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_v4_timing import MARKET, setup

from prop_production.evidence import sign
from prop_production.v4_market_identity import (
    load_display_catalog,
    validate_independent_quotes,
    validate_release_markets,
)


def catalog(tmp_path):
    body = {
        "schema": "paper_public_physical_sockets_v1",
        "native_market_count": 2,
        "propr_order_ids_attested": False,
        "paper_admitted": False,
        "physical_sockets": [
            {
                "name": "A",
                "instruments": [
                    MARKET,
                    "hyperliquid/mainnet:perp:xyz/BTC",
                ],
            }
        ],
    }
    path = tmp_path / "catalog.json"
    raw = json.dumps(body, sort_keys=True).encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def fixture(tmp_path):
    projection, release, source = setup(tmp_path)
    projection.close()
    path, pin = catalog(tmp_path)
    market_set = load_display_catalog(path, pin)
    issuer = Ed25519PrivateKey.generate()
    body = {
        "schema": "follower_quote_book_v1",
        "account_binding": release.payload["account_binding"],
        "release_sha": release.release_sha,
        "known_at_ms": 1000,
        "provider_endpoint": "https://example.test/native-quotes",
        "provider_scope": "independent-account-binding",
        "quotes": {
            MARKET: {"bid": "99.9", "ask": "100.1", "known_at_ms": 1000, "venue_native_id": MARKET}
        },
        "source_kind": "INDEPENDENT_FOLLOWER_VENUE_BID_ASK",
        "mutation_authorized": False,
    }
    return release, source, issuer, market_set, body, path, pin


def verify(release, source, issuer, market_set, body, **updates):
    return validate_independent_quotes(
        sign({**body, **updates}, issuer, "follower-independent-quote"),
        quote_key=issuer.public_key(),
        source_key=source.public_key(),
        release=release,
        catalog=market_set,
        now_ms=1001,
    )


def test_pinned_public_157_native_catalog_preserves_dex_scope():
    root = os.environ.get("PROP_RESEARCH_ROOT")
    if not root:
        pytest.skip("PROP_RESEARCH_ROOT must pin the verified copytrade source")
    path = Path(root) / "docs/FORWARD-PAPER-PROPR-157-THREE-SOCKETS-20261105.json"
    markets = load_display_catalog(
        path,
        "37735151bb825169e1c0283e6bfaf3dedb77897dc7ce83b4477f91961ae4b835",
    )
    assert len(markets) == 157
    assert "hyperliquid/mainnet:perp:default/BTC" in markets
    assert "hyperliquid/mainnet:perp:xyz/SP500" in markets
    assert "hyperliquid/mainnet:perp:xyz/BRENTOIL" in markets


def test_exact_dex_identity_and_independent_broker_quote(tmp_path):
    release, source, issuer, market_set, body, path, pin = fixture(tmp_path)
    assert len(market_set) == 2
    validate_release_markets(release, market_set)
    assert verify(release, source, issuer, market_set, body)[MARKET]["bid"] == "99.9"
    assert "hyperliquid/mainnet:perp:xyz/BTC" in market_set
    assert "hyperliquid/mainnet:perp:default/BTC" in market_set
    with pytest.raises(ValueError, match="PIN_DRIFT"):
        load_display_catalog(path, "0" * 64)


def test_no_source_signed_price_or_base_ticker_collisions(tmp_path):
    release, source, issuer, market_set, body, *_ = fixture(tmp_path)
    with pytest.raises(ValueError, match="SIGNER_NOT_INDEPENDENT"):
        validate_independent_quotes(
            sign(body, source, "follower-independent-quote"),
            quote_key=source.public_key(),
            source_key=source.public_key(),
            release=release,
            catalog=market_set,
            now_ms=1001,
        )
    wrong = copy.deepcopy(body)
    wrong["quotes"] = {
        MARKET: {**body["quotes"][MARKET], "venue_native_id": "hyperliquid/mainnet:perp:xyz/BTC"}
    }
    with pytest.raises(ValueError, match="IDENTITY"):
        verify(release, source, issuer, market_set, wrong)
    wrong = copy.deepcopy(body)
    wrong["quotes"] = {"BTC": body["quotes"][MARKET]}
    with pytest.raises(ValueError, match="COVERAGE"):
        verify(release, source, issuer, market_set, wrong)


def test_stale_tampered_spread_and_unverified_catalog_fail_closed(tmp_path):
    release, source, issuer, market_set, body, path, pin = fixture(tmp_path)
    with pytest.raises(ValueError, match="IDENTITY_OR_CLOCK"):
        verify(release, source, issuer, market_set, body, known_at_ms=0)
    wrong = copy.deepcopy(body)
    wrong["quotes"][MARKET]["ask"] = "150"
    with pytest.raises(ValueError, match="SPREAD"):
        verify(release, source, issuer, market_set, wrong)
    wrong = copy.deepcopy(body)
    wrong["quotes"][MARKET]["bid"] = "0"
    with pytest.raises(ValueError, match="SPREAD"):
        verify(release, source, issuer, market_set, wrong)
    envelope = sign(body, issuer, "follower-independent-quote")
    envelope["payload"]["quotes"][MARKET]["bid"] = "101"
    with pytest.raises(ValueError):
        validate_independent_quotes(
            envelope,
            quote_key=issuer.public_key(),
            source_key=source.public_key(),
            release=release,
            catalog=market_set,
            now_ms=1001,
        )
    raw = path.read_bytes()
    corrupted = json.loads(raw)
    corrupted["physical_sockets"][0]["instruments"].append(MARKET)
    altered = json.dumps(corrupted, sort_keys=True).encode()
    path.write_bytes(altered)
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        load_display_catalog(path, hashlib.sha256(altered).hexdigest())
