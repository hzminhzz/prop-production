"""Cross-repository native Propr -> Guardian gateway contract (fixture-only)."""
import asyncio
from datetime import datetime
from decimal import Decimal

import pytest

from prop_production.gateway import ReadOnlyGateway, inspect_propr_readonly

httpx = pytest.importorskip("httpx")


NOW = int(datetime.fromisoformat("2026-11-06T03:00:00+00:00").timestamp() * 1000) + 100


def test_native_evidence_never_invents_guardian_cash_or_history():
    pytest.importorskip("connectors.propr.native_evidence")
    ProprRest = pytest.importorskip("connectors.propr.rest").ProprRest
    methods = []

    def handler(req):
        methods.append(req.method)
        path = req.url.path
        if path.endswith("/book-account-issuances"):
            data = []
        elif path.endswith("/challenge-attempts"):
            data = [{"accountId": "selected", "attemptId": "attempt-1"}]
        elif path.endswith("/accounts/selected"):
            return httpx.Response(200, json={
                "accountId": "selected", "updatedAt": "2026-11-06T03:00:00Z",
                "balance": "1000.0000000001", "availableBalance": "400.1",
                "totalUnrealizedPnl": "-10", "isolatedPositionMargin": "100",
                "crossPositionMargin": "0", "crossOrderMargin": "0",
                "isolatedOrderMargin": "0", "totalInitialMargin": "50",
                "totalMaintenanceMargin": "25",
            })
        elif path.endswith(("/orders", "/positions", "/trades")):
            data = []
        else:
            raise AssertionError("Unexpected endpoint")
        return httpx.Response(200, json={"data": data})

    async def run():
        rest = ProprRest("https://api.propr.xyz/v1", "fixture", transport=httpx.MockTransport(handler))
        try:
            return await inspect_propr_readonly(
                rest, "selected", "https://api.propr.xyz/v1", NOW,
            )
        finally:
            await rest.close()

    snapshot = asyncio.run(run())
    assert snapshot.equity == Decimal("1090.0000000001")
    assert snapshot.provider_balance == Decimal("1000.0000000001")
    assert snapshot.provider_buying_power == Decimal("400.1")
    assert snapshot.cash is None
    assert snapshot.financial_complete is False
    assert snapshot.history_complete is False
    assert snapshot.cutoff is None
    assert snapshot.evidence_schema == "propr_native_evidence_v1"
    assert snapshot.native_account_id == "selected"
    assert snapshot.native_attempt_id == "attempt-1"
    assert snapshot.native_account_source == "challenge"
    assert snapshot.provider_time == "2026-11-06T03:00:00Z"
    assert ("cash", "UNKNOWN") in snapshot.provider_financial_provenance
    assert "RECONCILIATION_UNCERTAIN" in snapshot.uncertainty_reasons
    assert snapshot.ready(NOW, 5000) is False
    read_only = ReadOnlyGateway(lambda: snapshot, snapshot.account_binding, snapshot.endpoint)
    assert read_only.observe(NOW) == snapshot
    with pytest.raises(PermissionError, match="REMOTE_MUTATION_UNAVAILABLE"):
        read_only.submit({"intent_id": "fixture"})
    with pytest.raises(PermissionError, match="REMOTE_MUTATION_UNAVAILABLE"):
        read_only.cancel("fixture")
    assert methods and set(methods) == {"GET"}


def test_native_evidence_partial_inventory_is_not_flat_confirmation():
    pytest.importorskip("connectors.propr.native_evidence")
    ProprRest = pytest.importorskip("connectors.propr.rest").ProprRest

    def handler(req):
        path = req.url.path
        if path.endswith("/book-account-issuances"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/challenge-attempts"):
            return httpx.Response(200, json={"data": [{"accountId": "selected"}]})
        if path.endswith("/accounts/selected"):
            return httpx.Response(200, json={
                "accountId": "selected", "balance": "1000",
                "totalUnrealizedPnl": "0", "isolatedPositionMargin": "0",
            })
        if path.endswith("/orders"):
            return httpx.Response(200, json={"data": [{"accountId": "other"}]})
        if path.endswith("/positions"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/trades"):
            return httpx.Response(200, json={"data": []})
        raise AssertionError("Unexpected endpoint")

    async def run():
        rest = ProprRest("https://api.propr.xyz/v1", "fixture", transport=httpx.MockTransport(handler))
        try:
            return await inspect_propr_readonly(rest, "selected", "https://api.propr.xyz/v1", NOW)
        finally:
            await rest.close()

    snapshot = asyncio.run(run())
    assert snapshot.positions == ()
    assert snapshot.orders == ()
    assert snapshot.cash is None
    assert "INVENTORY_UNVERIFIED" in snapshot.uncertainty_reasons
    assert snapshot.ready(NOW, 5000) is False
