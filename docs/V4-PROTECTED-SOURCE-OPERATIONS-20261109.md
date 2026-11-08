# V4 source-only protected SHADOW installation (additive; NO_TRADE)

## Operating boundary

Code introduced here is **not** an order route. This is an opt-in
**source evidence service** distinct from the six currently installed
prop-production services. It does not change the synthetic Guardian,
Planner, Broker or Propr/Bybit observers, nor authorize opening a paper
or demo position. `SourcePacketReceiver` verifies external source
signatures, immutable byte digests, source scope, cursor continuity and
the registered prospective release, then persists only eligibility
observations. Its response explicitly states
`guardian_financial_admission=BLOCKED_NOT_WIRED`.

The public Propr 157 instrument catalog is an allowlist, not proof of
account access. `v4_market_identity.load_display_catalog` requires the
SHA256 of the **original bytes** plus exact DEX-qualified markets.
`validate_independent_quotes` requires another trusted key (distinct
from the V4 source signer), independent account-bound provider bid/ask,
freshness under five seconds, correct DEX-qualified native IDs and
independent covered markets. Neither Vike candles nor source marks
are provider bid/ask. There is currently no eligible real Propr
bid/ask publisher or independently qualified coherent financial cutoff.

## Required external approvals (not self-issued)

1. **Owner-approved signed prospective release:** preserve research SQLite
   inventory and sealed portfolio identity. Use the existing
   `copytrade_pm.production_export` and `production_manifest.store_policy`
   contract with a separately provisioned research reviewer signature,
   complete selected-account scope attestation, signed fixed-k and
   independent approver. Activate a separate prospective trust floor
   through the existing `prop_production.release.ReleaseFloor` **only**
   after independently reviewed approval. A synthetic fixture,
   historical paper profile or service configuration is not a release.
2. **Source-observer signer:** an independently reviewed and provisioned
   Ed25519 private key readable only by `quant` (0600); provision and pin
   the matching public key independently as root-owned 0600 at
   `/etc/prop-production/v4-source-observer.pub`. Do not reuse
   unrelated operator/macro-regime keys or trust a public key written by
   the unprivileged source process.
3. **Independent quote authority:** separate independently pinned quote
   key, verified provider-specific bid/ask and native contract metadata.
   Absence is a blocking result, never fallback to source `mark` or
   Vike OHLC. Approval of a public display catalog cannot grant an
   executable order route.

Do not change root-owned files from a quant-owned shell or bypass
permissions to get a test to pass.

## Source producer (quant only)

From the *verified, pinned copytrade-pm release* (not the mutable
development worktree), provision a quant-private 0700 source spool root
with 0700 outbox. The sealed selected-account map is regenerated on
**every** capture from current `P1,P14,P16` registry evidence. The first
packet is an empty signed baseline anchored at the current journal head,
never a retroactive entry. Run `python -m copytrade_pm.v4_source_spool`
with `--registry-db`, `--journal-db`, `--spool-root`,
`--source-signing-key`, `--source-public-key`, and
`--portfolios P1,P14,P16`. A cursor and signer mismatch fail closed.
Use a separate quant-owned user timer after first preflight; the existing
source observer need not be restarted.

## Root-mediated immutable inbox (separate from Guardian services)

Provision the protected inbox and state as separate paths, without
reading or writing the current Guardian journal or its rollback floor.
Example ownership (root operator, **not** auto-executed):

```text
/home/quant/.local/share/copytrade/prospective/v4-prop-source/outbox/
    quant:quant 0700; immutable producer packets quant:quant 0600
/var/lib/prop-production/v4-source/inbox/
    prop-runtime:prop-runtime 0700; packets prop-runtime:prop-runtime 0600
/var/lib/prop-production/v4-source/state.sqlite
    prop-runtime:prop-runtime 0600; fresh source-only projection
/etc/prop-production/v4-source-observer.pub
    root:root 0600; reviewed independent source key pin
/etc/prop-production/v4-portfolio-release.json
    root:root 0600; independently approved signed prospective manifest
/etc/prop-production/v4-release-approver.pub
    root:root 0600; independent reviewer key
/var/lib/prop-production/v4-source/approved-floor.sqlite
    separately reviewed and activated prospective release floor
```

The root-only copy action is
`python -m prop_production.v4_source_handoff` with
`--source-outbox`, `--protected-inbox`, `--source-public`,
`--source-user quant`, and `--recipient-user prop-runtime`.
It validates source ownership, original signatures and full file SHA,
fsyncs, then publishes a destination file with exclusive `os.link`.
No overwrite, chmod of source, broker connection or privileged write to
quant's source journal is permitted.

The protected, unprivileged `prop-runtime` one-shot consumer is
`python -m prop_production.v4_source_receiver` with `--inbox`,
`--state-db`, `--release`, `--source-public`,
`--approver-public`, `--release-floor`, `--account-binding`,
`--risk-policy-sha`, and `--rulebook-sha`.
All contract, clock, scope, signer, trusted risk/rulebook and independent
release-floor validation must pass before any signed source packet is
ingested. Missing trust files produce a structured `BLOCKED` result.

No `sudo` privilege was available to the `quant` development identity
when inspected. These protected installation steps require the real root
operator, dedicated approved credentials, and subsequent service
verification. Do **not** silently add these commands to the existing
six-unit `deploy/install.sh` bundle or change the root-owned Guardian
runtime while external evidence remains unprovisioned.

## Tests and negative controls

```bash
uv run --frozen --group dev pytest -q \
  tests/test_v4_source_receiver.py \
  tests/test_v4_source_handoff.py \
  tests/test_v4_market_identity.py \
  tests/test_v4_timing.py \
  tests/test_v4_guardian_shadow.py
```

Repeat committed cross-repository `scripts/verify-local.sh all` with
pinned research and connector worktrees. Filesystem ownership/IPC
deployment tests must be repeated by root after actual approved
provisioning; unit tests with test-only key material do not establish
trusted producer provenance or independent provider quotes.
Never claim deployed, financially admitted or scientifically
qualified from the source-only receiver.
