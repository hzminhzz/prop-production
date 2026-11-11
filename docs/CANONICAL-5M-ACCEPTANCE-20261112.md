# Guardian acceptance map for the approved CWSA five-minute strategy

**Date:** 2026-11-12
**Status:** DESIGN-ONLY. Owner-approved Q1–Q24 (Q2=B, Q13=B, all other A). No protected code, release floor, broker credentials, real account orders or service deployment was changed.

**Cross-repo design:** `copytrade-pm/docs/CANONICAL-5M-DESIGN-CLOSURE-20261112.md` (reference by repository + path, not a runtime filesystem link). **Binding local ADR:** [Guardian checkpoint policy](ADR-CANONICAL-5M-CHECKPOINT-GRILL-20261111.md). **Existing invariant constraints:** [protected invariant grill closure](ADR-INVARIANT-ENFORCEMENT-GRILL-ROUND5-20261111.md).

## 1. No implicit state machines and no new financial authority

Use the existing Guardian financial journal and verified native broker evidence. Separately prove CWSA's source continuity and each signed copy-mandate generation. A source target is **not** follower inventory, a financial intent, a broker order, a risk permit or a HALT reset.

The prospective financial consumer tracks one explicit **opportunity** per changed checkpoint and mandate generation. The opportunity's state is different from existing Guardian order statuses. All legal edges must be explicit guarded owner-local journal transitions, not inferred combinations of `is_active`, `status=green`, time elapsed, missing rows, socket readiness or source account health.

| Owning aggregate | Allowed conceptual transitions | Impossible shortcut |
| --- | --- | --- |
| CWSA (source-side, independently verified) | UNANCHORED → CONTINUOUS (baseline); CONTINUOUS → FENCED on gap; FENCED → CONTINUOUS (nonactionable recovery baseline); verified continuous changed checkpoint stays CONTINUOUS | Source recovery cannot create new exposure or reset Guardian broker UNKNOWN |
| Signed copy mandate | New generation AWAITING_BASELINE → ACTIVE on first complete baseline; ACTIVE → PAUSED or RETIRED; resume/revise creates a **new** AWAITING_BASELINE generation | Pause/retire or replacement does not imply flat account or rewrite old financial obligations |
| Consumer new-risk opportunity | New valid changed signal → REVIEWABLE; verified no-dispatch transient block ↔ REVIEWABLE; REVIEWABLE → COMMITTED_ONCE on persisted intent or → MISSED on expiry/terminal refusal | Unchanged checkpoint, late event or broker UNKNOWN cannot recreate opening |
| Existing Guardian broker order | Preserve current version-specific queued/possible-dispatch/ack/partial/UNKNOWN/terminal financial journal and independent HALT recovery | No second order state owner, no blind retries, no clearing HALT via healthy source |

## 2. Q21–Q24 protected policy consequences

- **Old mandate residuals (Q21=A):** Existing positions, pod attribution, fills, fees, reservations, UNKNOWN and HALT remain financially accountable to the old generation. Successor is independently baselined and may open only with coherent **total** follower exposure and no conflicting unresolved effect. Old positions are never double-counted or implicitly reattributed.
- **Opening expiry (Q22=A):** Immutable expiry = earlier of next UTC 5m boundary and a separately signed source/financial freshness deadline. A pre-dispatch recheck cannot extend it. At boundary equality, expire the old opportunity before processing the new checkpoint. Missing next-source input never extends the old window.
- **Native reversal (Q23=A):** One guarded cross-zero net order only when venue-native semantics, supported position mode, cumulative fills, outstanding/reserved exposure and all pod/account risk prove it safe. Otherwise separately admit *only verified reduce-only* and record opposite opening MISSED. No queued second opening under the same checkpoint.
- **Cutover (Q24=A):** Old and new strategies may coexist for independent read-only/SHADOW paper comparison. **Exactly one** signed opening policy effective per real account/pod. The switch requires future-effective epoch, verified nonactionable baseline, exact pins, independent operational/economic/account proof and separately authorized Guardian admission. Rollback preserves all signed receipts/journals/orders/HALTs/trust floors. Incompatible old binary → disarm, not downgrade.

## 3. Concrete protected acceptance oracles (not tests claimed to have passed)

| Test family | Must pass | Must fail / conservatively hold |
| --- | --- | --- |
| Auth/source | Valid native 14-account all-required-DEX complete signed CWSA proof and correct policy/epoch/sequence | Missing DEX/flat inference, forged/expired/wrong-signing key, clock skew/late proof, source mismatch |
| Mandate | Valid signed k, permitted market, source-to-pod/account identity, first activation baseline, version pin | Reused old-generation release, implicit copy-current, changing follower without signed epoch, overlapping effective opening policies |
| Opportunity | Exactly one identity and possible dispatch per changed checkpoint/consumer, pre-dispatch temporary correction before immutable deadline | Replayed change, unchanged target, expiry resurrection, new opportunity from recovery baseline, recheck after possible dispatch |
| Financial | Conservative broker UNKNOWN plus durable reservation; cumulative native fills and net pod attributions reconcile; risk/HALT/limits independently enforced | Broker ACK taken as terminal; cancel/fill race ignored; duplicated effect; unverified equity/cash, wrong fee/pod attribution |
| Reversal | Independently proven native-safe one-order cross-zero; otherwise reduce-only with recorded missed opposite | Automatic second opening or blind split, cross-zero under unknown old order |
| Isolation | Faulty wallet B fences B only; A/C can continue if account risk/market/source permits | Shared signer compromise or global account uncertainty incorrectly treated as B-only |
| Migration | Dual readonly comparisons, separate paper science and signed future effective one-policy live cutover; reversible compatible rollback | Shadow auto-arming, live dual-open, old T+300 receipt reinterpreted, journal/trust floor rewind |
| Scientific | Matched prospective actual-known economics with fees/slippage/delayed exit/late full-target entry/real drawdown reported separately | Engineering test pass or hypothetical midpoint P&L presented as live alpha |

**Measurement required before release:** Exact native source capture-time/cross-DEX grace, API rate/resources, original checkpoint age and due window, live market freshness, broker orders/positions/clearing cutoffs and true cumulative terminality, costed follower economics and an explicit independently signed account trading permit. None is established by this document.

## 4. Current code mismatch

`release.ReleaseVerifier` currently accepts strict `portfolio_release_v1` with `batch_seconds=300`. `v4_timing.V4ShadowProjection` generates independent T0+300 eligibility. `financial_semantics.validate_transition` verifies existing history/financial legality but not a new CWSA checkpoint policy or signed copy-mandate/opportunity transitions. The Execution Session remains diagnostic/untrusted. These components are **not** implementations of Q1–Q24, and zero-capital existing modes must remain zero-capital absent separate authorization.

**Design verdict:** Architecture complete for this grill; **engineering NOT IMPLEMENTED, operational NOT VERIFIED, scientifically NOT QUALIFIED, trading NOT AUTHORIZED**. Continue through a separately approved implementation work unit with risk-weighted adversarial tests and versioned release acceptance, not extra services or hidden state toggles.
