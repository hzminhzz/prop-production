# V4 official-position → Prop-Production SHADOW integration

## Engineering boundary

`copytrade-pm` is authoritative for the **official, exact selected-account**
Hyperliquid perps and the immutable observer stream. The additive
`v4_signed_source_export_v1` producer verifies the original SHA-linked
`PaperJournal`, raw native frame, DEX census, account hash and original
first-local-known clock before externally signing an event proof and, where
native marks exist, a `source_book_v1`. Vike minute OHLC is **not** an
executable quote or fabricated source mark.

`prop_production.v4_timing.V4ShadowProjection` independently verifies the
externally pinned source public key, `portfolio_release_v1` selected-account
scope, Ed25519 envelope, monotonic observation sequence, source clock and
continuity. It stores one local SQLite projection with a strictly private
parent and immutable release/cutoff. It does **not** own financial inventory,
equity, orders, broker credentials, or risk permits.

A genuine prospective position change creates a durable T0 per source market;
increases before due coalesce without resetting T0; the eligible target is
published at T0+300 s. Source reductions/reversals produce immediate
`REDUCE_NOW` eligibility; opposite-direction new risk requires a new 300 s
clock. A source GAP, unknown/partial native mark, stale state, or reconnect
blocks new risk and does not claim source flat. Initial baselines cannot open
risk. An unpriced observation consumes the source cursor while fencing pending
new exposure. Sequence replay and conflicting signed cursor are rejected.

`Guardian.preview_v4` is intentionally protected and **SHADOW only**.
Guardian authenticates the projection authority, independently reads account
finance, broker reconciliation, quote freshness, pending obligations, HALT,
release trust floor, risk policy and signed fixed-k sizing, and persists
`guardian_v4_shadow_decision_v1` into the existing hash-chained financial
journal. This receipt's `SHADOW_PREVIEW` status cannot pass the existing
`reserve` predicate; it is not an order intent or broker instruction. A
`BLOCKED` result records the exact independently observed reason. Existing
legacy Planner/Guardian and broker worker contracts remain unchanged.

One source identity can belong to multiple frozen signed pods, with
independent k and loss budgets. `compile_pods` now verifies once per selected
source and emits one target per pod; release verifier still rejects conflicting
scope for a shared source identity.

## What is implemented vs. not installed

Implemented: signed V4 source export and persistent transition eligibility,
Guardian SHADOW risk preview, regression tests, cross-Python producer→consumer
integration test and compatibility pin update.

**Not implemented as a deployed authenticated runtime:** protected root-owned
signed prospective portfolio admission, separately reviewed external source
signer/public-key installation, narrow authenticated quant→root evidence
transfer, native Hyperliquid-to-Propr market identity normalization, and a
protected production listener that durably consumes exported packets. The
existing `prop-production-planner.service` is still the legacy synthetic
Planner; this module does not replace it. The existing synthetic Guardian
remains the only installed broker simulation. Do not claim deployed integration
or live T+300 until these boundaries are separately wired and verified.

The protected Propr native account observations currently report
`RECONCILIATION_UNCERTAIN`: do not derive cash from equity, pretend REST
fills are history-complete, invent executable bid/ask, or clear HALT. No broker
mutation (real or demo) is authorized.

## Verification and publication

Focused executable checks:

```bash
uv run --frozen --group dev pytest -q tests/test_v4_timing.py \
  tests/test_v4_guardian_shadow.py tests/test_v4_shared_source.py
PROP_RESEARCH_ROOT=/absolute/clean/pinned/copytrade-pm \
  uv run --frozen --group dev pytest -q tests/test_v4_crosstree_integration.py
```

The source subprocess uses its pinned **Python 3.14** environment, and the
protected consumer uses Python 3.13; source imports never enter the protected
Planner/Guardian runtime. Integration uses invented test identities and keys,
not live account attestations. Build the application and connector from the
exact clean companion commits in `compatibility.json` and follow
`docs/LOCAL-VERIFICATION.md`.

SHADOW deployment remains forbidden until the protected read-only transfer
and owner-approved signer/release are installed, and reversible release
verification has been completed. Keep existing V4, V3, CCXT, Grafana,
Guardian and funded-service processes unchanged during preparation.
