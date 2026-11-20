# Changelog

All notable changes to `prop-production` are documented in this file.

## [Phase D & E — Unified Kernel Cutover] - 2026-11-20

### Added
- `packages/copytrade-kernel`: zero-dependency, `mypy --strict` Python 3.13+ stdlib package hosting `clocks`, `money` (`NativeInstrument`, `FixedKSizing`), `rulebooks` (`RulebookCatalog`), `mandates` (`CopyMandateSpec`, disjoint `MandateLifecycle`, `OpeningMissed` Q14–Q24 ADTs), `accounting` (`account_step`), `risk`, `decision`, and `kernel` (`step_checkpoint`, `effective_policy`, `NetOrderIntent`).
- `src/prop_production/checkpoint_receiver.py` & `checkpoint_kernel.py`: extracted wire-V3 `CheckpointSourceReceiver` and deep-immutable `KernelState` boundary adapter.
- `scripts/verify_legacy_v1_journal.py` & `tests/test_verify_legacy_journal.py`: standalone read-only verifier for pre-cutover `portfolio_release_v1` archives.
- `tests/test_checkpoint_kernel_boundaries.py` & `tests/test_checkpoint_q15.py`: AST boundary checks and Q15 opening-retry regression coverage.
- `src/prop_production/judge/broker.py`: configurable `FakeBroker.fee_rate` (default `Decimal("0.001")`) supporting cross-engine golden parity tests without altering production broker defaults.

### Changed
- Consolidated Guardian admission, risk, execution projection, mandate lifecycle, and challenge rulebooks onto `copytrade_kernel`.
- Updated `src/prop_production/compatibility.json` to pin `copytrade_pm` Phase D/E revision (`e1ec715016bbd9f2b84de0bae46bfa3b25324638`), wire V3, `portfolio_release_v2`, and `checkpoint_decision_q15_v1`.
- Updated `AGENTS.md`, `README.md`, `docs/SIZING-POLICY.md`, and `docs/LOCAL-VERIFICATION.md` to reflect the deleted Planner, retired `ALLOCATION_OVER_LIVE_NAV`, and `copytrade-kernel` single authority.

### Removed
- Deleted dead V1/V4 paths and legacy Planner (`v4_timing.py`, `v4_source_handoff.py`, `v4_source_receiver.py`, `planner.py`, `deploy/prop-production-planner.service`, and associated V4/planner test modules).
