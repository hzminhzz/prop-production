# Checkpoint cross-repository verification (#325)

## Exact matrix

The authoritative `src/prop_production/compatibility.json` pins research `a18c8c0f7af9851cfec2f9588a92e501331d1fff` and connector `ea95f9bf9661bcda41685d6ba939aa349628f8f5`. The containing application Git commit completes the matrix. The connector optional dependency and lockfile use the same exact pin. Contracts explicitly retain portfolio release v1 and add v2, checkpoint packet v3 and copy mandate v1. The default active source wire stays 1; declaring compatibility does not change the installed legacy policy.

Run from the clean application checkout:

```sh
PROP_RESEARCH_ROOT=/absolute/pinned/copytrade-pm \\
PROP_CONNECTOR_ROOT=/absolute/pinned/trading-connector \\
bash scripts/verify-local.sh all
```

The gate refuses dirty/mismatched companions, runs application Python 3.13 and 3.14 tests, lint, strict typing, CLI status/smoke and builds, connector/provider tests, research tests/lint/format/build/capability validation, and rechecks all three heads and cleanliness before its final receipt. Skipped credential-dependent tests are not live qualification. The final `LOCAL_VERIFY_PASS` receipt, not this document, is the acceptance evidence.

## Actual producer seam

`test_checkpoint_crosstree.py` executes `checkpoint_paper_producer.py` with the pinned research interpreter and research fixture namespace in a separate process. It uses the actual native checkpoint collector, CWSA journal, signed exporter and existing PaperCampaign. One canonical baseline and genuine change feed 16 independently signed synthetic paper accounts with distinct frozen k. All share the same source signal and three source transitions (register, baseline, change); all financial heads remain independent. Reopen and duplicate processing produce identical financial state.

The exported packets must equal the committed producer golden vectors. The protected receiver ingests those same signed packets. A Decimal/lot oracle independently checks full-target interpretation; a matching Guardian owner receives that target immediately within the checkpoint's own validity window. `due()` remains empty for wire 3: no extra legacy T0+300 scheduler exists. The Guardian path performs review only; replacing its fixture gateway with the actual ReadOnlyGateway prevents reservation and leaves no intents or broker submissions.

These are synthetic account/risk/quote/capability fixtures. The 16 names exercise consumer isolation; their synthetic k values are not approvals for the real selected portfolio campaign.

## Adversarial coverage

- Research checkpoint source/journal/runtime/spool: complete DEX census, empty books, late/cross-boundary state, invalid native bytes/decimals, gaps, duplicate/reorder/equivocation, process lease, bounded REST wave and durable cursor publication.
- Protected receiver: source/account/mode/policy/signature/scope binding, native original proof, baseline/nonopening recovery and signer-wide versus wallet-local fences.
- Mandate/admission/netting/reversal/generation suites: explicit lifecycle, activation baseline, immutable k/ownership/risk obligations, expiry equality, bounded prerequisite retry, same-market claim, pod gross risk before netting, source exits, cross-zero capability fallback, partial/cancel, UNKNOWN, old residuals and signed protective reductions under preserved HALT.
- SIGKILL suites: source and paper commits, Guardian reservation/send, and reversal. Restart keeps original owner, basis, one-shot terminality and reservations until an independent broker cutoff confirms the effect. The extended crash tests reopen Guardian using retained public trust and signed release evidence, reconcile the confirmed fill without resubmission, release the resolved reservation and preserve the HALT.

## Review correction

New red tests demonstrated that seven altered retained account-proof projections were not rejected by financial replay. `checkpoint_account_proof` now retains the complete normalized broker snapshot, rederives its projection, validates original typed financial completeness, cash/equity/positions, independent marks/quote sides, capabilities and the exact reconciled cutoff. Admission and protective replay use that same proof boundary. A changed provider cutoff requires fresh reconciliation even if cash and current positions happen to match. This is evidence validation, not another readiness state or financial journal.

Before the clean full matrix, the focused checkpoint/mandate set passed **85 tests**, strict mypy checked **51 source files**, and Ruff/diff checks passed. TDD began with explicit contract/pin failures and seven account-proof corruption failures. Full matrix results are recorded separately with the exact application SHA.

## Clean-install correction

The first clean Python 3.13 matrix leg exposed an optional-dependency leak: the synthetic broker imported the native connector SDK, producing 93 failures after `uv sync` removed the optional package. The broker now independently emits capability wire evidence; the application owns its verifier without importing the optional SDK. An exact-pinned cross-process test checks agreement with the connector parser on valid, unknown, expired, hedge-mode, wrong-account and malformed evidence. A legacy orphan-fill error contract was also restored. The corrected complete core application run passes **321 tests / 1 skipped**, with strict typing across **52 source files** and Ruff passing. The full clean multi-interpreter/provider/research receipt remains the final gate.

## Qualification boundary

No protected installation, production source cutover, native order, account permission or economic qualification follows from this suite. Native execution capability is still UNKNOWN unless separately evidenced. Long-run throughput and prospective matched economics are separate from bounded fixture engineering correctness.
