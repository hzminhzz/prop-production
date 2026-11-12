# Specification #313 — direct two-axis review

Review scope: the complete nonactivating implementation and its integration
surfaces, followed by focused corrections. Standards and specification were
assessed separately. Execution was direct; no delegated coding/review agent or
external safety certification is claimed.

## Fixed baselines and evidence

- copytrade-pm: `79af6e523c31fdf9c6257e0b39c3e21c6cc1fad9...HEAD`
- prop-production: `3f3481961f3bd15c8381d18ef9b69e9cb633d83c...HEAD`
- trading-connector: `d768d10f13beaf293f0218666fa40d291d018664...HEAD`

The implementation heads entering final review were research
`a18c8c0f7af9851cfec2f9588a92e501331d1fff`, application
`00e8fb3127dfb1b1527876bdce13943aea68e457`, connector
`ea95f9bf9661bcda41685d6ba939aa349628f8f5`. Final companion pins come from
`src/prop_production/compatibility.json`; the application pin is the containing
Git commit. A clean full matrix must validate those exact revisions after this
review. PR #48 retains its commit-bound receipt.

Standards sources: all three AGENTS.md contracts, repository architecture and
owner runbooks, the immutable original-evidence/PM03/PM04 contracts, protected
local verification requirements, explicit transition ownership, signed
release/rollback rules and the accepted merge-versus-promotion ADR. Spec sources:
parent #313, #314–#328 and approved Q1–Q24.

## Standards

**Corrected:** stale AGENTS/README statements still called the implemented
checkpoint path unimplemented. They now point to the owning implementation,
source measurement, integration and release evidence while preserving distinct
installed-policy and qualification gates.

Ownership remains inside the existing collector/observer, PaperJournal/PM03,
signed release verifier, source receiver, Guardian/financial journal and native
connector. No new service, trading scheduler or financial ledger was introduced.
Typed transitions cover source continuity, mandates, opportunities, market
claims, release rebinding, protective reductions and code recovery. Raw native
scope/clocks, Decimal quantities, signer floors and old-generation financial
ownership remain explicit.

The producer and protected consumer intentionally validate independently.
The journal oracle replays original evidence and accounting rather than
accepting a planner's risk label. Optional native SDKs remain outside the
application core. All new broker-mutating fixture paths retain explicit
synthetic-account/rulebook/gateway fences.

Maintainability note: checkpoint admission and replay verification are sizeable
modules. Their financial owner and evidence boundaries are cohesive; splitting
them solely to reduce file length would not remove a demonstrated failure mode.
Long-lived source-history replay throughput has not been operationally measured.

## Specification

**Corrected P1 — retained-owner freshness:** runtime prerequisites and protective
reduction derivation read the successor's looser freshness settings while old
exposure retained its original contract. New tests demonstrated both acceptance
of a 3000 ms quote against the retained 2000 ms fixture limit and an intent
deadline extended beyond the original account-evidence limit. Runtime admission,
reservation and independent protective replay now intersect the retained owner
contracts through the existing effective-policy owner. The two regression tests
and 34 surrounding lifecycle/protection/account-proof/admission tests pass.
These numbers are synthetic test inputs, not operational freshness approvals.

**Test coverage completed:** the real selected-campaign facade now explicitly
rejects a changed frozen k, preserves the rejected account's exact journal, and
lets the other 15 valid baselines remain isolated. Missing approvals and changed
mandates have independent assertions.

Earlier integrated adversarial corrections are preserved: original account
proof replay, provider-cutoff changes, clean-install optional-SDK isolation,
foreign mandate/control signer rejection, mandatory receiver genesis,
safe cross-zero/reduce-only fallback, immutable market claims and real SIGKILL
reconciliation without resubmission.

**External evidence remains incomplete:** #317 operational source policy and
long-tail/shared-IP coverage, #326 actual matched prospective follower ledgers
and approved criterion, and #327 the real trusted future candidate. No fixture
or historical T0+300 result satisfies those criteria. #328 must publish separate
capability dispositions and retain the corresponding NO-GO decisions.

No remaining blocking code finding was identified in this direct review scope.
This conclusion is bounded engineering evidence. It does not qualify native
broker semantics, prospective economics, account permission or deployment.

## Reproduce the corrections

```bash
.venv/bin/pytest -q tests/test_checkpoint_retained_protection.py tests/test_checkpoint_generations.py tests/test_checkpoint_protection.py tests/test_checkpoint_admission.py tests/test_checkpoint_account_proof.py
```

Research: `pytest -q tests/test_checkpoint_campaign.py` (3 passed). Full acceptance:
run `bash scripts/verify-local.sh all` from the clean containing application
commit with the two exact clean companion roots. The final receipt must include
all three SHAs, compatibility digest, counts and skips; no dirty-tree run
substitutes for that gate.
