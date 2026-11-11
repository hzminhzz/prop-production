# prop-production execution contract

## Start

Inspect Git status, remotes, worktrees and the current GitHub ticket before edits.
Read `README.md`, then the owning module and its public behavioral tests.
ChatGPT executes directly; do not delegate unless the user explicitly asks.
Complete the OptMem wake pagination; use memory for durable navigation, then verify
live GitHub, SQLite and OS evidence. The actual default branch is currently
`feat/account-guardian-program`; recheck before publication.
Preserve other sessions' changes. Use isolated worktrees when upstream is dirty.

## Capability-scoped gates (2026-11-07)

Read [capability-scoped readiness](docs/CAPABILITY-SCOPED-READINESS-20261107.md). Credential-free source OBSERVE and read-only PAPER receipt collection are independent of protected Guardian/financial attestations, historical positive alpha, unrelated regime proofs and optional 72-hour soak. `read_only_collection=EVIDENCE_COLLECTED` is **not** paper-follower admission: preserve `BLOCKED_PAPER_ADMISSION` absent executable follower economics. DEMO/LIVE still require account-bound reconciliation, actual equity, signed release, risk, broker permissions and explicit authorization.

## Authority

Research metadata belongs to copytrade-pm SQLite. Provider protocol code belongs
to trading-connector. Guardian account finance belongs to this application.
Consume Propr read-only provider data exclusively through the versioned native
evidence contract in connectors/propr/native_evidence.py via BrokerGateway. Never
manufacture Guardian cash as equity minus unrealized PnL or treat matching REST
snapshots as a coherent provider history cutoff. Preserve provider financial
provenance, native identity, timestamps and RECONCILIATION_UNCERTAIN. No
connector-owned journal may replace a Guardian-committed production intent.
Bybit is a second, independently authenticated Demo-only read-only observer:
use the pinned BybitClient, explicit Demo UID/key/endpoint verification, and
BYBIT_MODE=read_only. Never fall back to BYBIT_PROP_* or mainnet/testnet
credentials. Bybit equity and available buying power are not ledger cash;
REST order/position reads are not coherent history. See docs/BYBIT-DEMO-INTEGRATION.md.
Planner cannot supply trusted broker equity, grant permits or access credentials.

## Approved future checkpoint lifecycle: explicit transitions only (2026-11-12)

The isolated [CWSA/Guardian design ADR](docs/ADR-CANONICAL-5M-CHECKPOINT-GRILL-20261111.md) records owner-approved Q1–Q24, **not implemented or trading-authorized**. Read [the local adversarial acceptance map](docs/CANONICAL-5M-ACCEPTANCE-20261112.md) before any future implementation. Reuse existing Guardian-owned finance and durable journals. Never implement an implicit state machine using `is_active`, `ready`, age alone, missing rows, service statuses or mixed concern enums. Future source continuity, signed mandate generations and new-risk opportunities must have separately owned typed predecessor/trigger/guard/next-state/durable-effect contracts; the existing broker order journal remains the only protected financial truth. Shared CWSA continuity does not reset when one follower's copy mandate changes; unknown orders and HALTs cannot be cleared by source health. No third supervisor, second Guardian or global FSM.

## Binding owner decision: source NAV is not a universal gate (2026-11-06)

- **Default copy policy:** risk-budgeted, fixed-k mirroring of an explicitly selected leader account/perpetual position scope. `FIXED_K_NATIVE_QUANTITY` is the signed release member policy: follower target **notional** is `k * leader native position quantity * source mark`, then quantity is determined at the independent follower quote and checked by Guardian. Source `nav=null` is valid in this mode and MUST NOT cause a `SOURCE_NAV` rejection or a request to reconstruct parent/subaccount/whole-wallet equity.
- **Legacy fidelity policy:** `ALLOCATION_OVER_LIVE_NAV` remains available *only when explicitly selected* to reproduce leader equity-relative exposures; that path still requires verifiable leader NAV. Do not apply its NAV requirement to fixed-k policies.
- **Mandatory regardless of policy:** authentic signer/release, canonical source/account scope, full positions inside the selected scope (not unrelated subaccounts), causal first-known clocks, signed fixed k, follower account equity/headroom, provider account reconciliation, fresh follower quotes, risk-policy caps, independent pod loss stop, daily/trailing drawdown, volume/lot/margin, HALT and order authorization. A $200 pod loss threshold does not guarantee execution at exactly $200 under gap/slippage.
- Do not promote source NAV from Vike wallet summary or sum per-DEX NAV for unified/portfolio-margin accounts. Missing NAV can be a diagnostic field but **not** a blocker for `FIXED_K_NATIVE_QUANTITY`. No source-NAV request should interrupt routine research, paper planning, or engineering on this mode. See `docs/SIZING-POLICY.md`.

Default SHADOW / NO_TRADE. No real broker mutations, credentials, wallet identities,
private account responses, datasets or signing secrets in Git.
Production permission requires account-bound independent evidence and explicit
owner authorization. A mode flag, test result or service restart never grants it.

## Execution Session diagnostics (2026-11-11)

Read [Execution Session v1](docs/EXECUTION-SESSION-20261111.md) before changing admission logic. `prop-production session --path` is a strictly read-only, account-scoped, *unverified-claims* diagnostic. It must not be consumed as a broker permit, financial attestation, or authority to clear HALT. Only the existing Guardian journal may authorize committed order intents. A healthy synthetic broker socket is not provider mutation readiness; current native Propr order authority is unavailable.

## Verification

For this private program, GitHub Actions is not required. Use the commit-bound
local verification script and acceptance contract in docs/LOCAL-VERIFICATION.md;
require a clean reviewed revision and both pinned companion suites before a merge.
`src/prop_production/compatibility.json` is the only companion/schema pin authority;
package constraints, runtime bridge, verifier and deployment must agree with it.
Read `docs/DEPLOYMENT-RUNBOOK.md` before protected installation. Bundle the app,
connector and units together; use named compatible rollback and never rewind journals
or the trust floor. The current symlink alone does not prove running process code.
Record exact commands, SHAs, counts, skips and reviewer. Never confuse a locally
passing suite with an independent safety certificate or trading authorization.
Test behavior through
CLI, TargetBatchProposal -> DecisionReceipt -> durable intent -> BrokerGateway.
Use exact decimal accounting; the Outside Judge must not import Guardian risk or
finance functions as its oracle. Never do network I/O in a SQL transaction.
Commit reservations and intent before dispatch. Unknown outcome is not failure.
Keep HALT across restart and midnight. Missing evidence means BLOCKED.

## Continue

Diagnose -> repair -> verify -> resume unrelated unblocked tickets.
Only close issues with evidence for every acceptance criterion. Record external
blockers precisely and keep IMPLEMENTED, ENGINEERING_VERIFIED, DEPLOYED,
RUNNING_OBSERVED, SCIENTIFICALLY_QUALIFIED, TRADING_AUTHORIZED separate.
