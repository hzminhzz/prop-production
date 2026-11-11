# ADR: Guardian invariant-enforcement grill, Round 3 — scoped failures and trusted recovery

**Date:** 2026-11-11
**Status:** **OWNER-APPROVED FOR DESIGN**, Q11=A through Q17=A. Documentation-only, not implemented, pushed, merged, deployed or trading-authorized.

## Context

Extends [Round 1](ADR-INVARIANT-ENFORCEMENT-GRILL-20261111.md) and [Round 2](ADR-INVARIANT-ENFORCEMENT-GRILL-ROUND2-20261111.md). Protected defaults remain SHADOW/DISARMED/NO_TRADE. The existing broker snapshot only bounds signed filled quantity, not FILLED == full executed quantity; recovery checks the evidence digest shape; V4 source projection reads its cursor before a write transaction; the packet receiver can commit a prefix before a packet completion receipt. None of those are fixed by this design record.

## Q11 — Admissible source mark precedence (APPROVED: A)

Prefer a fresh, authentic official native leader valuation, with native contract/DEX identity and validated source clock at the T0+300 cutoff. A mark computed from positionValue / abs(szi) is candidate evidence but does **not** refresh its original observation time. If it is stale or missing, a separately authenticated leader-venue mark may be used only under an explicit versioned source-mark policy binding price semantics, native instrument, signer/provider, cutoff and independent freshness. Never substitute follower midpoint, Vike OHLC or historical fills for a leader mark. Missing evidence blocks **new-risk**, not verified protective exposure reduction. Specific thresholds/source cadences remain undecided.

## Q12 — Contradictory broker evidence is a financial account HALT (APPROVED: A)

Incoherent order status, cumulative fills, provider history cutoff, positions or reservations latches an affected **protected account** new-risk HALT. A FILLED order with only 60% confirmed execution is not terminally conserved. Preserve known fills, residual orders/reservations, held inventory and unknown outcomes. Broker acknowledgements do not release risk or permit replacement orders. Unrelated source observers/research continue. Protective reductions require independently reconciled actual holdings and admissible market/risk evidence; do not bypass risk authority merely because a HALT exists.

**Rejected:** quarantining one intent while permitting unrelated new-risk activity on the same incoherent protected financial account.

## Q13 — Preserve versioned historical semantics (APPROVED: A)

Validate each original journal transition under the rules/proofs available in its actual historical version, plus unmodified SHA and sequence checks. A missing proof required for a newer invariant becomes **LEGACY_UNVERIFIABLE** for the affected capability, not automatic corruption or certification. Read-only forensic access remains; independently verified forward financial reconciliation must establish a safe new-risk boundary. Never rewrite SHA chains, discard reservations, clear durable HALTs or reduce the trust floor to force upgrade acceptance. Exact event-kind verifier, checkpoint semantics and transition-version handling remain Round 4 decisions.

**Rejected:** destructive journal replacement; unconditional acceptance of all hash-valid legacy economics.

## Q14 — Protected recovery signer plus independent remediation predicate (APPROVED: A)

A dedicated role-scoped, pinned protected recovery authority signs operator recovery **requests** with account, exact HALT reasons, incident ID, journal head/sequence, epoch, trust/release policy, bounded expiry, nonce and evidence references. This proves who requested action, **not** actual remediation. Guardian separately verifies cause-specific authentic provider/finance/source evidence, non-replay and absence of conflicting open effects. Denied versus requested versus verified must be separately auditable. Verified clearance of the final cause revokes permits, advances boot epoch and returns only to DISARMED; unrelated HALTs persist. Escalate exceptional high-impact overrides; no blanket dual signature for SHADOW. Custody, revocation, trust pin updates and exception classes remain Round 4 decisions.

**Rejected:** operator/digest strings as remediation proof; signature alone as evidence.

## Q15 — Signed per-stream cutoff/head anchor and prospective v2 proof (APPROVED: A)

At a separately approved prospective cutover, pin a signed per-source stream-head anchor with source identity/selected DEX scope, release/signer, epoch/generation, committed cursor, verified original chain head and explicit cutoff/first-known boundary. Only newly accepted v2 predecessor-linked successors may extend that trusted head. Validate predecessor, current event proof and accepted cursor under the destination's owning transaction. Historical v1 signed bytes remain immutable with their original assurance and release-floor semantics. An anchor attests continuity **after** the trusted cutoff, not complete pre-anchor source history; baseline cannot mint retroactive entry signals. Mixed-schema compatibility and key rotation are Round 4 design work.

**Rejected:** genesis replay as a universal prospective requirement; silently treating v1 as predecessor-verified.

## Q16 — Late T+300 never permits new-risk catch-up (APPROVED: A)

Keep original first-local-known T0 and frozen attempt identity. If T0+300 plus the approved original policy grace has elapsed without verified modeled/real attempt, record EXPIRED/MISSED and do not create a new-risk catch-up trade or new T0 after restart. An uncertain previously dispatched broker action stays UNKNOWN until independently reconciled; no re-dispatch with a new identity. A protective reduction is distinct and may proceed only with independently confirmed actual inventory, market evidence and existing protective-risk authority. Source-only receiver never grants order permission.

## Q17 — Authentic packet envelope, then account-specific event quarantine (APPROVED: A)

Authenticate entire signed packet envelope and original payload digest first. If this common proof fails, reject the whole packet. If the envelope is valid but one wallet event's signature/cursor/scope/chain fails and the defect is attributable to that wallet, quarantine only that stream/dependents. Healthy independent streams may commit verified progress. Persist or derive PARTIAL packet status and each stream's exact accepted prefix; do not call the packet COMPLETE until all included events receive explicit audited valid disposition. Replays neither duplicate accepted effects nor skip the bad stream. Shared signer compromise or common transport failure fences all affected streams. Specific terminal disposition/retention remains Round 4.

Current SourcePacketReceiver verifies event signatures during whole-batch validation and then applies them sequentially: scoped quarantine is a **future design**, not a present guarantee.

## Rejected alternatives and next frontier

No follower midpoint as source mark, intent-only financial HALT, journal rewrite/legacy evidence promotion, signed-but-unverified recovery, v1-to-v2 cryptographic retroactive upgrade, late catch-up entries or global packet abort for one isolated invalid wallet.

Round 4: exact source mark timing/provenance, status normalization and reservations, semantic replay/checkpoint, protected recovery roles/rotation, v2 anchor/dual-read migration, packet quarantine disposition/retention and adversarial proof/reversible release sequencing. Guardian remains protected finance owner. **No implementation, push, merge or deployment was authorized by this ADR.**
