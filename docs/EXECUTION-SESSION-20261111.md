# Execution Session v1 — nonactivating evidence assessment

## Ownership and safety

`src/prop_production/execution_session.py` is a **read-only, account-bound diagnostic**. It is not a Guardian, broker gateway, release signer, risk writer, financial journal, or permission mint. Invoke `prop-production session --path <private-json> --now-ms <epoch-ms>`. The CLI returns exit 2 when required claims are absent or inconsistent.

A valid-looking JSON claim is **not** an independently verified provider receipt. Its success label is `CLAIMS_COMPLETE_UNVERIFIED`, **never** `TRADING_AUTHORIZED`. Both permitted and blocked assessments return `authorization_issued=false`, `trading_authorized=false`, `mutations=0`. Actual Guardian execution continues to use the existing authoritative signed release, financial journal, reservations, risk, broker reconciliation and dispatch protections. Do not make Guardian consume this untrusted diagnostic as a permit.

## Scope

The input declares `scope` with `venue`, `environment`, `account_binding`, `release_sha`, `source_scope_sha`, and `market_scope_sha`. Source and market must match those scopes, be native/continuous/complete, and be fresh. For PAPER, it additionally demands causal T+300, verified fixed-k release and follower economics qualification claims. DEMO/LIVE additionally require native broker identity, coherent account positions/pending-order/history, real mutation capability, broker epoch, Guardian journal/halts/release match, follower finance and risk checks, owner authority and G0–G4 attestations. LIVE requires separate scientific qualification. No leader NAV requirement is imposed for the fixed-k mode.

Every assessment is hashed over the complete input, emits evidence-category digests, a five-second assessment expiry marker, an explicit blocking-reason set and no raw account identity. The expiry marker is a diagnostic validity bound; **not** an execution permit TTL.

## What remains to implement before trading

1. Consume *verified* Vike-first selected native complete-position receipts from the copytrade-pm migration. The current Vike dual observations/REST reducer alone do not prove a reconciled continuous book.
2. Connect independent native quote receipts and authentic source events to Guardian's protected decision lifecycle. Keep T0 first-known immutable and exactly one T+300 attempt under the signed release.
3. Add a native, documented Propr order/reconciliation transport if the authenticated challenge account actually permits it, retaining Propr read-only isolation until then. Bybit Demo remains a distinct venue.
4. Bind G0–G4 signed attestations and protected Journal reads to the session assessor using trusted local adapters; never accept input booleans as permission.
5. Build journal-atomic, single-use, short-lived intent capability issuance **inside Guardian only**, with replay and epoch-change protection. Extend adversarial runtime transport and fault tests before enabling it.
6. Deploy only after clean pinned cross-repository verification, independent finance checks and authenticated incident recovery. No time-based HALT clearance.

## November 11, 2026 runtime diagnosis

The VPS six-unit SHADOW release `2cfb70e282bfdf5f7a874db8028003480c970d3b` was running at inspection. `/var/lib/prop-production/broker/b.sock` was listening under `prop-runtime`, mode 0600; broker and Guardian run as that same user. Protected operator status reported `COHERENT` with fresh financial progress, but `BROKER_UNREACHABLE` remains a **persistent historical HALT**. The watchdog source/market contract is still hardcoded `MARKET_SOURCE_UNKNOWN`. The order route is `NONE`. These are inspection observations, not a readiness grant. The existing live provider mutation route remains unavailable by design and connector contract; do not confuse synthetic IPC success with Propr order authority.

The assessment is safe to merge independent of scientific or live release activation because it cannot mutate accounts. Its tests do not qualify LIVE or DEMO.
