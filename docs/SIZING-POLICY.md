# Signed copy-sizing policy — owner decision, 2026-11-06

**Status:** architectural admission contract implemented in Planner / release verifier, tested with signed *synthetic* fixtures. No live broker order authorization, no production deployment. This document supersedes the claim that leader whole-account NAV is universally mandatory.

## Default: fixed-k risk-budgeted mirroring

The follower mirrors the **signed notional of the explicitly selected account's perpetual position vector**, not the source wallet's entire net-worth or balance sheet. When source/follower marks differ, follower quantity is not exactly k times source quantity. The release's signed member contains:

- `sizing_policy = FIXED_K_NATIVE_QUANTITY` (historical field name; its enforced unit is signed **position notional**)
- `k`: explicit positive signed Decimal ratio, bounded by release contract (`0 < k <= 10`); calibrated from a versioned research plan, not guessed from leader equity
- `source_identity`, `account_scope`, `allowed_markets`, `identity_known_at_ms`, `allocation`: unchanged account-bound release commitments
- A first-known, signed, SHA-bound `source_book_v1` with complete positions **within that selected scope**, event/receipt timestamps, valid native position quantities and marks, and `nav = null`.

For each observed source leg:

```text
requested_notional = signed_k * source_qty * source_mark
target_follower_qty = requested_notional / verified_follower_mark
```

A target is **an intent**, not an order. Guardian re-verifies source signature, signed k and all target hashes and applies actual follower account equity, headroom, stressed cost, per-pod loss budget, market volume/quantity/price tiers, margin, price freshness, global limits, netting, broker accounting and HALT. Partial-fills and skipped follower legs must reconcile against actual positions, not presumed previous trades. `allocation` is retained as a signed member allocation/metadata field, **not** an alternative k denominator or an authorization to exceed Guardian constraints.

Complete *selected scope* does not mean reconstructing every unrelated parent or subaccount. If the copied strategy spans multiple named child accounts, those scopes must be explicit and captured. Do not silently omit selected positions, hedges or markets. Source-only spot/vault positions are not follower perp signals. A parent-only policy must not claim it copied a source's unrelated child exposures.

## Fidelity mode: explicitly opt-in

`ALLOCATION_OVER_LIVE_NAV` (legacy v1): signed source NAV must be positive and independently verified; follower exposure equals source quantity × source mark ÷ source NAV × follower allocation. This mode remains strict to preserve source-equity-relative fidelity studies and existing release compatibility.

**Missing leader NAV does not block fixed-k research or planning.** Keep whole-account NAV as an optional descriptive/fidelity diagnostic. Do not generate synthetic NAV or collapse the two policies into a hidden fallback.

## Separate admission gates

Regardless of k or NAV policy: verify canonical wallet identity and selected source account scope, complete within-scope position snapshots, fresh first-local observation, valid native assets, follower quotes and tradeability, follower actual equity, pod risk budgets, daily/trailing loss floors, order/reservation lifecycle, provider reconciliation and explicit trading authorization. A configured $200 pod loss cutoff triggers intended flatten/freeze, but is **not** a maximum realizable loss under slippage, gaps, liquidation or order failures.

No source NAV inference from Vike `summary`, spot quote collateral, unified/portfolio-margin per-DEX balance sums or unsynchronized parent/child snapshots.

## Compatibility and tests

The existing signed release `portfolio_release_v1` still accepts only known member fields. Fixed-k members must additionally carry `k`; equity-relative members must **not** carry it. Cross-policy changes alter the signed `definition_sha` and release digest and must pass the trusted release flow. Production route remains disabled by default. Guardian independently recomputes proposals from signed source receipts, and tests reject unsigned k changes, invalid k, incomplete selected scope and stale/mismatched clocks.

Do not promote passing synthetic tests to real source or financial eligibility. Existing production gates unrelated to leader NAV remain independent and are not waived.
