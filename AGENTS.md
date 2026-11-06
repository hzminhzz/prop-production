# prop-production execution contract

## Start

Inspect Git status, remotes, worktrees and the current GitHub ticket before edits.
Read `README.md`, then the owning module and its public behavioral tests.
Preserve other sessions' changes. Use isolated worktrees when upstream is dirty.

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

Default SHADOW / NO_TRADE. No real broker mutations, credentials, wallet identities,
private account responses, datasets or signing secrets in Git.
Production permission requires account-bound independent evidence and explicit
owner authorization. A mode flag, test result or service restart never grants it.

## Verification

For this private program, GitHub Actions is not required. Use the commit-bound
local verification script and acceptance contract in docs/LOCAL-VERIFICATION.md;
require a clean reviewed revision and both pinned companion suites before a merge.
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
