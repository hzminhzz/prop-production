# Native Propr connector integration and migration

**Scope:** prop-production PR #23 + trading-connector PR #18, TC-01..TC-09. SHADOW / DISARMED / NO_TRADE.

## Integration contract

The application's `inspect_propr_readonly(rest, account_id, endpoint, received_at_ms)` uses `connectors.propr.native_evidence.inspect_native_evidence` (`propr_native_evidence_v1`). Version, account and endpoint binding are checked before mapping. The connector returns exact provider amount/provenance, native account/attempt and inventory records, local-known time and provider time. The application retains provider balance and available buying power as diagnostic fields, but **never invents the cash ledger** from `equity - unrealized`.

The provider SDK derives equity with the isolated margin formula, not necessarily `balance + unrealized`. Guardian's accounting invariant for its synthetic broker cannot be projected onto native Propr finances without independently verified accounting semantics. Consequently the mapped snapshot has `cash=None`, `financial_complete=False`, `history_complete=False`, `cutoff=None`, `epoch=0`, and `connected=False` unless a future separately verified transport contract establishes connection. Provider balance/buying power, native identity, timestamp and explicit uncertainty reasons remain available for non-authorizing diagnostics. `native_inventory_read_complete` and `native_inventory_count` describe a bounded REST read, **not** confirmed account flatness. Missing native prices, fee, order-ID associations and execution timestamps preserve explicit uncertainty rather than creating fabricated fills.

The independent Guardian synthetic fake-broker path retains its preexisting fixtures and permission rules. Real `ReadOnlyGateway.submit` and `cancel` always reject; no private account mutation, ML4T LiveEngine or additional order journal is introduced. A Guardian intent is stored transactionally before synthetic dispatch. The optional connector's legacy ML4T journal is not Guardian authority.

## Verification and release

- Default application: `uv run --frozen pytest -q` on supported Python 3.13/3.14, Ruff, strict mypy and build. This runs without ml4t-live.
- Cross repository: `PYTHONPATH=<verified connector checkout> uv run --frozen --extra connector pytest -q tests/test_native_gateway.py`, and `uv run --frozen --extra connector` for installed optional connector.
- Connector: native Propr tests, optional ML4T 0.1.2 contract checks and independent Bybit/Hyperliquid adapter tests. Ultimate SDK HTTP mutation spy is a mandatory negative control.
- Clean pinned acceptance: `PROP_RESEARCH_ROOT=<pinned research> PROP_CONNECTOR_ROOT=<pinned connector> bash scripts/verify-local.sh all`. The script checks SHA, cleanness, 3.13/3.14 app, Propr connector and research dependency. Do not treat a development `GATE_CLEAN=0` run as acceptance.
- Review complete PR diffs, branch protections and all TC-01..TC-09 issue criteria before merging. Existing draft Guardian PR #23 has its **own unresolved acceptance gates**, independent of this connector integration.

## External unresolved blockers

Propr has no established coherent REST+WS historical cutoff or replay guarantee; matching offset reads do not prove account flatness. Actual challenge/API classification and account-specific rulebook, provider cash semantics, missing fee/funding and position evidence, server idempotency and explicit authorizations remain unverified. Keep uncertain outcomes UNKNOWN, no blind resend. Passing synthetic/credential-free tests does not grant scientific qualification or trading permission.
