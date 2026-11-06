# Pinned Propr provider audit

Inspected upstream main 95ea7d28343de3678777751bd6a1defcb0b00e55 and patched
candidate ec3f03a5d24a2db3b0c75fe61e3a3fbfa662b343 in an isolated worktree.
Original dirty checkouts were preserved.

## Actual interfaces

- ProprRest.request(method, path, **kwargs) and pages(path, page_size=20):
  async GET/HEAD/OPTIONS only, bounded offset pagination, no automatic retries.
- ReadOnlyAccount.inspect(): explicit or unique account discovery, exact Decimal
  financial fields. availableBalance is buying power, not equity.
- InventoryReader.snapshot(account_id=None): native orders, positions and trades,
  duplicate-ID and cross-account validation.
- ProprClient.create_order(..., client_intent_id=None): patched to require the
  caller's persisted ID; batch calls require IDs and do not modify input data.
- ProprLiveBroker.submit_order_async(asset, quantity, side=None, order_type=...,
  limit_price=None, stop_price=None, **kwargs): installed ml4t-live 0.1.2 signature
  inspected. It passes the durable journal ID to the SDK.
- Real SDK Session.send rejects every mutation even when the SDK helper or
  ProprConfig is bypassed. Only a reserved fixture.invalid callback is synthetic.

## Confirmed defects repaired upstream

Independent local/transmitted intent IDs; SDK direct mutation paths; first-active
account selection; optimistic HTTP 400 cancel interpretation; blind replacement
after an unproven cancel/fill cutoff; missing root SDK packaging.

## Remaining unknown evidence

No account-specific credential or account ID is inferred for this application.
Propr Beta/Trial naming does not prove zero-capital routing. Account-specific
daily reset, mark convention, maintenance tiers, product orderability and
server-side deduplication/query-by-intent are UNVERIFIED. Offset REST pages and
WS observations lack a proven coherent replay cutoff. The adapter therefore
returns RECONCILIATION_UNCERTAIN even if individual fields appear complete.
429 and timeout do not grant readiness or trigger mutation retries.

The four-week minimum paper policy remains an external gate. No live/dedicated
demo provider mutation is enabled by this pin or by passing fixture tests.
