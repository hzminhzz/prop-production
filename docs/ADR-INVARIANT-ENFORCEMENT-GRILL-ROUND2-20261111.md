# ADR: Guardian invariant-enforcement grill, Round 2

Date: 2026-11-11
Status: OWNER-APPROVED DESIGN (Q5=A, Q6=A, Q7=A, Q8=A, Q9=A, Q10=A); documentation only, NOT a code fix, source release, migration, merge, deployment or trading authorization.

## Context

Builds on [Round 1](ADR-INVARIANT-ENFORCEMENT-GRILL-20261111.md) and the existing synthetic Guardian, read-only source receiver and SHADOW-only operations contract. The relevant implementation still has an unverified broker status-to-quantity relationship, a hash-valid but not universally semantic Guardian journal, metadata-only Journal.recover evidence checks, pre-BEGIN source cursor reads, and per-event packet prefix commits. This ADR does not claim those findings have been remediated in runtime.

## Q5: Independent leader source mark at decision time (APPROVED, A)

The existing signed FIXED_K_NATIVE_QUANTITY policy means follower USD notional = selected leader signed native quantity * an independently authenticated source-venue mark * frozen k. Convert that notional using separately authenticated, fresh **follower** quote evidence and native precision, then apply all Guardian account/risk limits. A mark inferred from the official source book's native positionValue / abs(szi) is a candidate only with original source provenance, contract match and validated freshness at the decision cutoff. A separately sourced leader-venue mark needs an explicitly versioned trust policy. Neither stale source marks, follower midpoints, public candles nor source position values alone qualify as executable destination bid/ask. Missing required evidence blocks new-risk but cannot imply a flat existing position. Whole-wallet leader NAV is not a universal gate.

## Q6: Broker terminal status must conserve executions (APPROVED, A)

- FILLED implies full signed cumulative execution and independently coherent fill/order proof for the bound intent.
- Other terminal statuses can coexist with partial fills only when provider semantics and complete cutoff history support those fills; the unfilled remainder or uncertain remote outcome must not disappear from financial accounting.
- The gateway validates provider schema/status grammar and impossible terminal combinations. Guardian independently conserves its own recorded execution, inventory and reservations. Contradictory terminal data, orphan fills, bad cutoff or UNKNOWN outcome cannot release reservations, substitute a new order or issue a permit.
- Preserve risk-off ability subject to independently verified positions/quote and existing protections. Account financial incoherence blocks new-risk admission without shutting down unrelated public source collection.

Rejected: status-only reservation release.

Current code: src/prop_production/gateway.py BrokerSnapshot.ready does not require FILLED cumulative quantity to match full order quantity; src/prop_production/guardian.py _reconcile_economics uses terminal status to release reservations when no errors are found.

## Q7: Versioned domain-semantic journal replay (APPROVED, A)

Every protected commit and every verified historical transition must satisfy legal account-bound state transitions and finance postconditions applicable to its **original** event/version. Existing SHA-chain, sequence, writer fence and immutable event bytes remain authoritative and unchanged. Older events without adequate semantic data are classified conservatively as legacy-unverifiable where appropriate: allow investigation, disallow unsupported new-risk authority until independently reconciled. Do not erase or rewrite old financial events. A successful hash check alone is not proof of HALT, permit, reservation or fee correctness. Preserve network-I/O-free transaction boundaries. Specific v1 migration and verifier acceptance semantics are Round 3 decisions.

## Q8: Authenticated operator receipt plus separately verified remediation (APPROVED, A)

An authenticated, signed recovery request must bind protected account, operator authority, incident, precise HALT reason, original journal state/epoch, expiry and verifiable evidence identity, and resist replay. Its signature authenticates *intent to request recovery*, not the evidence's correctness. Guardian must validate cause-specific remediation predicates against separate trusted evidence (for example coherent financial provider history and current unresolved-order state) before recording VERIFIED or clearing a latch. An unrelated HALT remains. After last verified clearance, revoke permits, advance boot epoch and remain DISARMED; never auto-arm or dispatch. Existing MIDNIGHT_BASELINE_UNKNOWN reset stays narrow. Avoid universal two-person approval; high-impact exception review remains separately scoped. Exact trust anchor and step-up are Round 3 decisions.

Rejected: current metadata-only signature/digest shape as a substitute for actual proof.

## Q9: Prospective v2 predecessor-linked signed stream (APPROVED, A)

Future source wire records bind the prior chain identity, current event digest, account/scope/epoch/cursor, first-known clock and approved signer in authenticated signed bytes. Receiver verifies predecessor linkage against *its own* durable expected stream head within one local transaction (or equivalent provable writer fence). Do not treat a producer-signed current chain_sha as independent predecessor proof. Existing v1 historical bytes, signatures, release floors and previous assurance levels remain immutable and explicitly versioned. New bootstraps need a signed stream-head anchor at deployment cutoff; exact compatibility migration is Round 3.

Rejected: trust-only current chain without verifiable predecessor.

## Q10: Independent, deterministic consumer effect recovery (APPROVED, A)

Model source receipt and local consumer effects as different authorities with separate durable prefixes and immutable IDs. Each destination journal uses a deterministic source-event + run/portfolio/pod + frozen policy/release effect ID, and commits local consumption/effect atomically where supported. Crash-before-effect leads to reconciliation of this original ID, not a new synthetic T0 or an order retried under a different ID. A conflicting or uncertain effect remains quarantined and risk accountable. Do not impose multi-database two-phase commit or call at-least-once source delivery an exactly-once financial execution. Timing-expired new-risk and late protective reduction behavior will be resolved next round. Protected source projection remains NO_TRADE.

## Rejected alternatives and consequences

No source/follower price substitution, broker-status-only terminality, hash-only semantic replay, self-attested HALT recovery, silent v1 cryptographic upgrade, distributed commit or automatic post-crash trading catch-up.

Round 3 frontier: source mark provenance precedence/age; contradictory order/fill consequences; legacy v1 replay ambiguity; operator recovery trust design; signed genesis/cutover anchor; missed T+300 and protective replay. After that: concrete versioned records, compatibility migration, adversarial fault matrix and separate release prerequisites. No current engineering acceptance is asserted.

Relevant modules: src/prop_production/gateway.py; guardian.py; journal.py; v4_timing.py; v4_source_receiver.py; docs/V4-PROTECTED-SOURCE-OPERATIONS-20261109.md.
