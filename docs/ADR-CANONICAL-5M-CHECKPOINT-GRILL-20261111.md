# ADR: Shared five-minute source signals, Guardian financial independence

**Date:** 2026-11-11
**Status:** OWNER-APPROVED TARGET ARCHITECTURE (Grill Q1=A, Round 2 Q3=A/Q4=A/Q5=A); Q2 PROVIDER AUTHORITY OPEN. Documentation-only, not implemented, deployed, scientifically qualified or trading-authorized.
**Companion:** `copytrade-pm/docs/decisions/2026-11-11-canonical-5m-checkpoint-grill.md`.
**Current installed contract:** `V4ShadowProjection` uses original transition T0+300 and remains unchanged.

## Approved boundary

The planned Canonical Wallet Signal Authority (CWSA) in `copytrade-pm` owns one verified selected-account position checkpoint every five UTC minutes and one versioned source target-state signal identity. It compares complete, provenance-backed economic position states; a changed sampled target may be published once with original checkpoint/cutoff and proof. It does **not** own the follower account or authorize orders.

The existing Guardian is the sole **protected financial authority**. The proposed Proof-Carrying Execution Lane is a deeper lifecycle **inside Guardian**, not a new service or risk engine. It validates independently authentic source receipts and native market/account evidence, verifies the active signed policy and frozen k, applies account risk and HALT, durably commits intents before dispatch, and reconciles broker orders, fills and uncertain outcomes. It may verify a checkpoint and freshness but may **not** manufacture a second leader clock or alternative source signal identity.

Paper PM03/PM04 remains independent simulated finance. Each consumer owns its source cursor and financial effect identity, not source truth. Shared signal data is not a broker order, a Guardian permission or a global financial transaction. Signed qualification, owner authorization and account-specific native permission remain separate admission gates. Read-only OBSERVE must not require LIVE financial qualification.

This new policy differs economically from existing first-transition T0+300: source changes near a five-minute boundary may be acted upon with substantially less than 300 seconds of age. Intra-window round trips may be invisible. Do **not** promote existing prospective/backtest T+300 evidence as evidence for this policy. Do not weaken unknown-outcome recovery, broker reconciliation, protected HALT, journal nonrewind, or financial conservation.

## Additional owner-approved policy decisions and Guardian implications

- **Q3=A:** Each UTC five-minute source checkpoint is evaluated using a fresh, complete, verified observation obtained within a bounded window; retain separate checkpoint, provider-state, local-receipt and validation/publish timestamps. The maximum age and grace limits are not yet approved. Never transform a late/replayed snapshot into an earlier known time.
- **Q4=A:** A CWSA signal is the complete desired source-account position vector, not an executable order delta. Guardian independently verifies the source receipt, frozen signed k, native source/follower valuations, its own account holdings, unresolved broker effects and risk. Repeated target receipt cannot bypass idempotent intent/execution reconciliation.
- **Q5=A:** Source-driven increases, reductions, closes and reversals all follow the same five-minute checkpoints. Account loss limits, HALT, incident-driven protective risk reduction and unknown-outcome recovery continue to operate independently between checkpoints. Delaying source closes changes economics and requires separate prospective and costed qualification; no automatic trading permit.

**Q2 remains open after live Vike review.** Vike REST/MCP supports wallet-filtered 5-minute position history and an approximately five-minute latest all-wallet positions archive. However, source-state `state_ts` can precede ingestion `snapshot_ts` by 5–10 minutes; snapshot writes are irregular, files appear later, older stale rows can represent recently closed positions and native quantity fields use Float64. Do not label a provider position row an account-complete, fresh or exact signed checkpoint without independent proof. Official per-checkpoint Hyperliquid REST may bypass the existing user-WS subscription budget but requires separate all-DEX completeness and rate/cutoff verification. See the companion CWSA ADR for evidence and alternatives.

## Unresolved contracts for next grill

**Settled:** fresh bounded source observation rather than falsely exact as-of sampling (Q3), full leader target-state signal (Q4), and five-minute-grid **source** reductions/entries with independent immediate financial protection (Q5). **Unsettled:** numeric freshness/grace and evidence lag; missing/stale/incomplete sample recovery; signal/source-epoch identities; target convergence versus one attempt; reduction/reversal and partial-fill disposition; cross-consumer proof version; provider authority (Q2); exactly-once local financial intent versus uncertain remote outcome; signed release and reversible activation policy.

## Migration constraints

The currently installed V4 timing projection, Guardian journal, Planner/receiver, Execution Session diagnostic, protected services, broker routes and financial authorization remain unchanged. Future transition requires a **new signed policy version and prospective epoch**, independent compatibility review, matching 16-paper/Guardian source IDs, realistic follower economics and separate authorization. No historical receipt interpretation may change through this ADR.
