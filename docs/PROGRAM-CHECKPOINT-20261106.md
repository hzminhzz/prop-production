# Program checkpoint — 2026-11-06 (SHADOW / NO_TRADE)

This is an engineering checkpoint, **not** a release approval or completion certificate. Parent specification: GitHub issue #1; implementation: #2–#22; application PR #23; connector PR #18; research PR #127. All 21 implementation issues were observed **OPEN** at inspection. Do not close them from this checkpoint without reviewing the current issue comments and the full acceptance criteria individually.

## Exact reviewed revisions

- Application: `feat/guardian-runtime` through `4c59627ddb7a3d7e0ca827b52bdbbdff0236ce77`, followed by the in-progress verifier-label correction recorded in this checkpoint's eventual commit.
- Pinned connector worktree: `ec3f03a5d24a2db3b0c75fe61e3a3fbfa662b343` (PR #18, open).
- Pinned read-only research worktree: `fcf79b8e4d8012b46cdf8d2ad190af06a6e8dd5e` (PR #127, open).
- Both companion main checkouts are dirty: no modifications to them were made. Production application's branch was clean after each published batch.

## Verified checks and limits

- Application Python 3.13: `PROP_RESEARCH_ROOT=/home/quant/.devspace/worktrees/copytrade-pm-dade3502 uv run --python 3.13 --frozen pytest -q` — **76 passed**; cross-repository signed-manifest integration was exercised.
- Application Python 3.14: `PROP_RESEARCH_ROOT=/home/quant/.devspace/worktrees/copytrade-pm-dade3502 uv run --python 3.14 --frozen pytest -q` — **76 passed**, including cross-repository signed-manifest integration and the Outside Judge verdict-label correction.
- Application `uv run --frozen ruff check src tests`, `uv run --frozen mypy`, `uv build --no-sources`, `git diff --check`: passed after the verdict-label correction.
- Connector pinned worktree: `.venv/bin/python -m pytest -q connectors/propr/tests` — **138 passed, 2 skipped**.
- Research pinned worktree: `uv run --frozen --extra actuary --group dev pytest -q` — **1,068 passed, 12 skipped**; `uv run --frozen --group dev copytrade-pm capabilities validate` — **93 capabilities, 0 errors**.
- GitHub Actions jobs on all three PR heads failed before any job steps; local checks do not establish green GitHub CI. Prior billing-lock diagnosis is consistent but requires external resolution.
- Synthetic black-box Judge scenarios cover NORMAL, ACCEPT_THEN_TIMEOUT, OUT_OF_ENVELOPE. The extreme gap breaches the synthetic contract and now yields `NOT_EVALUATED`, not a bounded safety approval. These scenarios **do not** cover the required full fault/mutant matrix or create an independently signed G2 certificate.

## Issue disposition matrix

| Issue | Implemented / verified evidence | Disposition and unmet acceptance |
| --- | --- | --- |
| #2 | Runnable credential-free shadow app, status/smoke, build and local test matrix | OPEN; fresh-machine and GitHub CI evidence incomplete |
| #3 | Ed25519 signed release verifier, anti-rollback floor and negative tests | OPEN; complete approval/provenance review pending |
| #4 | Pinned read-only research SQLite exporter and app's signed consumer fixture integration | OPEN; real source NAV/scope and final registry approval missing |
| #5 | Single-writer SQLite FULL/WAL journal, replay, HALT, backup and fault tests | OPEN; complete long-reader/throughput/production storage fault matrix pending |
| #6 | Typed proposal/decision seam, source/clock checks, synthetic process isolation | OPEN; full hostile-client/IPC permissions and operational surface acceptance pending |
| #7 | First-receipt anchored 300s planner and signed whole-book synthetic receipts | OPEN; complete real-source NAV/subaccount admission unavailable |
| #8 | Read-only provider gateway, pinned connector audit and fail-closed observation certainty | OPEN; account-specific provider evidence unavailable |
| #9 | Companion connector PR #18 durable intent identity and transport mutation guard; local suite passed | OPEN; server-side idempotency and actual provider semantics unverified; PR unmerged |
| #10 | Decimal broker-account floors, daily/trailing HALT rules, synthetic midnight tests | OPEN; authenticated Propr account reset/mark/rulebook terms missing |
| #11 | Full-book synthetic stress sizing, reserves, account/pod budget checks | OPEN; true broker margin/liquidity rules and wider correlated scenario matrix missing |
| #12 | Committed intent/outbox before synthetic dispatch, timeout remains UNKNOWN, duplicate controls | OPEN; expanded crash/cancel/fill adversarial acceptance pending |
| #13 | Broker cutoff/position reconciliation, unknown outcomes and restart-disarmed tests | OPEN; provider coherent cutoff, fee/funding/realized pod attribution and complete permutation matrix missing |
| #14 | Restart-disarmed, HALT latch, unknown/order recovery and process crash tests | OPEN; complete SIGKILL/recovery matrix and verified orphan reduction missing |
| #15 | Unprivileged Landlock/seccomp sandbox tests for synthetic workers | OPEN; standalone watchdog, root-protected multi-user systemd installation, rollback and incident acceptance not deployed |
| #16 | Hidden persistent fake broker and independent Fraction oracle plus Guardian projection comparison | OPEN; extended event matrix, seed coverage and independent reviewer verification pending |
| #17 | Abstract protocol model and bounded three-scenario separate-process Judge, negative-projection controls | OPEN; full fault schedule, seven required implementation mutants and signed certificate missing |
| #18 | Read-only status/portfolio/broker/risk/intent/periodic paper views, durable manual HALT | OPEN; actual protected watchdog/process identity and operational acceptance pending |
| #19 | Read-only prospective receipt-hash observation journal and explicit `BLOCKED_PAPER_ADMISSION` | OPEN; no admitted source-NAV/volume/market observations, live paper portfolio, unattended soak or independently matched follower PnL |
| #20 | Pinned research-native 20-day economics executable and read-only integration | OPEN; `BLOCKED_ECONOMICS` due to missing synchronized policy tapes, independent economics review, costs and contract terms |
| #21 | Fail-closed `BLOCKED_DEMO_PROVIDER` reporting path | OPEN; verified risk-free provider class and genuine demo canary evidence missing; no broker mutation allowed |
| #22 | G0–G4 signed evidence gate evaluator; `phase_c: BLOCKED` without independent attestations | OPEN; real G0–G4 evidence and separate authorization unavailable; no live route |

## Non-negotiable unresolved defects and blockers

1. Exact account cash is reconciled, and current fill fees are conserved across opposed pods. **Realized PnL and funding cannot yet be uniquely attributed to pods** without provider event/basis evidence. An unexplained cash debit, or a position close without verified pod basis, deliberately HALTs instead of fabricating attribution.
2. Synthetic zero-net opposing pod transfers are durable and do not send broker orders. This is not a qualified cross-pod netting solution for all partial-fills/cancels under independent real provider observation.
3. No protected production watchdog, independent service identities, root-installed `prop-production` systemd units, or complete operational rollback rehearsal was observed on the VPS. Existing copytrade research observers are not production execution workers.
4. Real signed source account-wide NAV and position scope, exact authenticated provider rulebook and endpoint permissions, prospective executable follower tapes and independently certified risk envelopes remain missing.
5. `BLOCKED_PAPER_ADMISSION`, `BLOCKED_ECONOMICS`, and G0–G4 `BLOCKED` are correct fail-closed outcomes. Green synthetic tests cannot promote them.
6. GitHub Actions billing/job-startup blocker must be resolved without bypassing required PR protections. No PR was merged in this batch.

## Final boundary

`IMPLEMENTED`: partial engineering slices; `ENGINEERING_VERIFIED`: only the specifically named local suites; `DEPLOYED`: false for prop-production; `RUNNING_OBSERVED`: false for prop-production; `SCIENTIFICALLY_QUALIFIED`: false; `TRADING_AUTHORIZED`: false. **No real broker order was sent by this program.**
