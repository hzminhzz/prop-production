# ADR: Guardian invariant-enforcement grill Round 4 — strict boundaries and reversible change

**Date:** 2026-11-11
**Status:** OWNER-APPROVED FOR DESIGN: Q18=A, Q19=A, Q20=A, Q21=A, Q22=A, Q23=A, Q24=A, Q25=A. Documentation only; not implemented, pushed, merged, deployed or trading-authorized.

## Context and authority

This ADR extends [Round 1](ADR-INVARIANT-ENFORCEMENT-GRILL-20261111.md), [Round 2](ADR-INVARIANT-ENFORCEMENT-GRILL-ROUND2-20261111.md) and [Round 3](ADR-INVARIANT-ENFORCEMENT-GRILL-ROUND3-20261111.md).

Only Guardian owns protected finance and broker admission; source timing is an independently signed **read-only** projection. The only cross-repository pin authority is src/prop_production/compatibility.json. Existing deployment documentation preserves immutable release bundles and journal/trust-floor nonrewindability. The V4 protected source receiver remains NO_TRADE with missing original market/finance proof.

## Q18 — Versioned source mark freshness (APPROVED: A)

Require an explicit leader price trust/age contract for each DEX-qualified native instrument, including provider, semantics, native source frame or separately authenticated source-venue quote, provenance SHA, first-known/as-of time, decision cutoff, measured uncertainty, maximum admissible age and fallback version. Prefer a fresh official native valuation; if missing/stale, admit a separate **leader-venue** mark only under preapproved versioned fallback provenance. The observer's derived positionValue / abs(szi) mark does not magically refresh five minutes later at T+300. Never use follower midpoint/OHLC/fill as a silent source mark. Follower bid/ask requires independent account/contract-specific admissibility with its own age/side rule. Insufficient evidence blocks only dependent new risk; source continuity and real inventory are not reset. Thresholds must be supported by measured provider cadence; this ADR authorizes no guessed seconds limit.

## Q19 — Provider-specific status normalization and Guardian conservation (APPROVED: A)

Normalize each real provider's original status, IDs and cutoff evidence into a separately versioned canonical order observation **at the provider boundary**. Preserve original status/raw provenance, signed quantity, verified cumulative executed quantity, execution IDs/fee/time, partial remainder, history completeness and canonical disposition. FILLED means full signed executed quantity with complete coherent evidence. CANCELLED/EXPIRED/REJECTED with partial executions can be terminal only under provider-specific proof and reconciliation of the actually filled portion; UNKNOWN or conflicting status cannot release reservations. Guardian independently validates canonical observations against its actual intent, broker order, fills, account/pod inventory and reservations before altering financial state. A contradiction latches account-scoped new-risk HALT without stopping unrelated source observation. The current Propr and Bybit readers lack a qualified coherent financial cutoff; do not claim a synthetic-only test certifies live provider statuses.

**Rejected:** sending raw arbitrary provider terminal strings into financial release logic.

## Q20 — Nonrewinding forward semantic checkpoint for historical finance (APPROVED: A)

Verify original-version historic transitions and unchanged SHA chain. Where older original proofs lack newly required economic properties, classify legacy-unverifiable and retain readonly investigation. A **new, append-only, independently evidenced forward semantic checkpoint** may be established by actual coherent provider/account reconciliation with explicit account, journal sequence/head, boot/release epoch, relevant financial obligations, open orders and evidence receipts. It must not rewrite v1 bytes, clear UNKNOWN/reservations or durable HALT by implication, restore an old financial baseline as new truth, or lower the trust floor. Relevant protected new-risk admission remains blocked until the checkpoint independently meets the Guardian authority's requirements; the checkpoint alone is not permission to ARM or trade.

**Rejected:** journal rebuild, historical resealing, direct v1 grandfathering, indefinite requirement for nonexistent v2 fields in past v1.

## Q21 — Dedicated scoped recovery signer and revocation lifecycle (APPROVED: A)

Use a distinct least-privilege **protected recovery request authority**, separately pinned and not shared with source observer, release approver, operator navigation credentials or broker secrets. Bind account/incident/reason, operator role, current journal head and epoch, policy/release identity, nonce and bounded expiry; independently check revocation and rotation. A genuine signature authenticates the request, not the remediation. Independently evaluate cause-specific actual financial/account/source evidence before VERIFIED clearance. Remaining HALTs stay latched; clearing the last one only DISARMS and advances boot epoch, never arms or dispatches. Exceptional high-impact override is separately reviewed and cannot fabricate missing finance evidence. Routine SHADOW recovery does not require universal dual signoff. Trust roots and secret custody remain outside Git; no key creation or privilege mutation is approved here.

## Q22 — Future-only v2 source handshake via existing compatibility manifest (APPROVED: A)

Require an explicitly pinned v2 signed source wire, signed producer cutoff/head anchor and verified predecessor-linked successors. The **existing application compatibility manifest** pins both producer/consumer commits and supported wire semantics; do not create a second registry. The protected consumer may verify historical v1 bytes for read-only provenance, but once a source epoch is anchored to v2 no live v1 fallback, ambiguous mixed-version stream or duplicated first-known opportunity is accepted. New epoch begins with a nonsignaling baseline. Preserve journal/trust floors, source scope and signing authority; install protected source-only consumer only under separately approved root-managed operations. No broader Guardian order route is granted by an authenticated packet.

**Rejected:** dynamic v1/v2 interpretation or silent downgrade; rewriting historical v1 evidence.

## Q23 — Distinguish COMPLETE from RESOLVED_WITH_QUARANTINE (APPROVED: A)

Verify whole-packet envelope/signature and original payload digest before local event processing. For an authenticated envelope, individually accepted wallet prefixes commit with their own cursors; an attributable invalid wallet remains PARTIAL/QUARANTINED without cursor skip or invented flat. A genuinely verified new official baseline plus separately auditable disposition of the invalid occurrence can close the **original packet** as RESOLVED_WITH_QUARANTINE, visibly distinct from COMPLETE (which requires valid acceptance of every original event). No rejected event becomes accepted economic history or new entry. Healthy independent wallets retain progress. Unscopable shared trust failure fences all affected dependents. Exact remediation state retention and stale-packet handling remain next-round design policy; a timer cannot silently manufacture completion.

**Rejected:** success after dropping wallet B, or global wallet HALT solely because B's attributable local event is bad.

## Q24 — Destination-owned causal effect receipt, no fictitious cross-DB atomicity (APPROVED: A)

For each paper source occurrence, bind the original committed source head/cursor, portfolio/run/pod, frozen policy/k/release, decision and first-known clocks, independent price evidence and deterministic local effect identity. Commit the destination consumer progress plus effect/terminal disposition within the destination owning transaction. Replay same identity no-ops, conflicting identity/unknown external result quarantines and preserves risk. A source transition after a final source-head read but before a separate financial commit remains a **cross-database ordering limitation**; prove only the committed as-of decision with recorded fence/evidence, and later reconcile changed source state under its own deterministic effect. Never assert a global lock or two-phase atomic commit. Source import/projection is never a broker permit. Synthetic paper guarantees do not transfer automatically to protected Guardian.

**Rejected:** global coordinator, regenerated attempts, uncontrolled catch-up trades on a process restart.

## Q25 — Economic semantic correction, frozen-k version and independent activation (APPROVED: A)

The corrected independent source/follower mark input is a new versioned economics contract. Keep prior frozen k, original report/signatures and historic calibrated/scientific claims unchanged. Run matched old/new PIT economic comparisons with identical genuine opportunities, native lot rounding, fees/slippage and all affected risk budgets; mark missing real source marks and unqualified follower economics explicitly. A source-price engineering fix and code merge do **not** independently authorize changing active k, releasing protected finance or trading. Any new active k/risk policy requires separately approved future-effective signed release, exact release/rollback identities and Guardian financial admission. Do not invent a parallel release registry or bypass existing Track C safeguards.

## Next frontier

Define acceptance: critical adversarial source/price/broker/recovery/concurrency failures with original authorities, clean companion-commit test matrix and skip policies, trust/proof-negative controls, scoped authenticated read-only SHADOW canary, source/consumer activation and rollback of code only, durable packet/quarantine retention, measured runtime budgets and separation of engineering/operational/scientific/trading authorization.

No implementation, key changes, root service installation, schema migration, push, merge or broker operations are approved by this document.
