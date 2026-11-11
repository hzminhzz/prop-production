# ADR: Shared five-minute source signals, Guardian financial independence

**Date:** 2026-11-11
**Status:** OWNER-APPROVED TARGET ARCHITECTURE (Grill Q1=A); documentation-only, not implemented, deployed, scientifically qualified or trading-authorized.
**Companion:** `copytrade-pm/docs/decisions/2026-11-11-canonical-5m-checkpoint-grill.md`.
**Current installed contract:** `V4ShadowProjection` uses original transition T0+300 and remains unchanged.

## Approved boundary

The planned Canonical Wallet Signal Authority (CWSA) in `copytrade-pm` owns one verified selected-account position checkpoint every five UTC minutes and one versioned source target-state signal identity. It compares complete, provenance-backed economic position states; a changed sampled target may be published once with original checkpoint/cutoff and proof. It does **not** own the follower account or authorize orders.

The existing Guardian is the sole **protected financial authority**. The proposed Proof-Carrying Execution Lane is a deeper lifecycle **inside Guardian**, not a new service or risk engine. It validates independently authentic source receipts and native market/account evidence, verifies the active signed policy and frozen k, applies account risk and HALT, durably commits intents before dispatch, and reconciles broker orders, fills and uncertain outcomes. It may verify a checkpoint and freshness but may **not** manufacture a second leader clock or alternative source signal identity.

Paper PM03/PM04 remains independent simulated finance. Each consumer owns its source cursor and financial effect identity, not source truth. Shared signal data is not a broker order, a Guardian permission or a global financial transaction. Signed qualification, owner authorization and account-specific native permission remain separate admission gates. Read-only OBSERVE must not require LIVE financial qualification.

This new policy differs economically from existing first-transition T0+300: source changes near a five-minute boundary may be acted upon with substantially less than 300 seconds of age. Intra-window round trips may be invisible. Do **not** promote existing prospective/backtest T+300 evidence as evidence for this policy. Do not weaken unknown-outcome recovery, broker reconciliation, protected HALT, journal nonrewind, or financial conservation.

## Unresolved contracts for next grill

Checkpoint sampling cutoff and evidence lag; missing/stale/incomplete sample treatment; signal and source-epoch identities; target convergence versus one attempt; protective decreases and reversals; cross-consumer proof version; provider authority choice (Q2, still open); exactly-once *local intent* versus uncertain remote outcome; signed release and reversible activation policy.

## Migration constraints

The currently installed V4 timing projection, Guardian journal, Planner/receiver, Execution Session diagnostic, protected services, broker routes and financial authorization remain unchanged. Future transition requires a **new signed policy version and prospective epoch**, independent compatibility review, matching 16-paper/Guardian source IDs, realistic follower economics and separate authorization. No historical receipt interpretation may change through this ADR.
