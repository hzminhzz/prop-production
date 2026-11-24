# prop-production

Account-guarded copytrading execution application. Default mode is **SHADOW / NO_TRADE**; default branch is `feat/account-guardian-program`.

## Hard guardrails

- **No secrets or PII in Git:** Keep credentials, wallet identities, private account responses, datasets, and signing keys outside Git.
- **Keep readiness dimensions separate:** `IMPLEMENTED`, `ENGINEERING_VERIFIED`, `DEPLOYED`, `RUNNING_OBSERVED`, `SCIENTIFICALLY_QUALIFIED`, and `TRADING_AUTHORIZED` are independent claims; passing tests or restarts never authorize trading.
- **Cross-repo pins:** `src/prop_production/compatibility.json` is the sole companion/schema pin authority.

## Ownership & invariants

- **Domain split:** `packages/copytrade-kernel` (shared decision engine, `RulebookCatalog`, `account_step`, Q14–Q24 mandates), Account Guardian finance, and durable intents belong here (`copytrade-pm` owns `FactStore`/`RosterStore`; `trading-connector` owns provider transports). Never place network I/O inside a SQL transaction or let a connector journal replace a Guardian intent.
- **Explicit typed transitions:** Keep explicit predecessor/trigger/guard/next-state/durable-effect contracts (`CWSA V3 -> checkpoint review -> durable intent -> BrokerGateway`). Commit reservations and intent before dispatch; persist `HALT` across restart and midnight; treat unknown outcomes as outstanding risk and missing evidence as `BLOCKED`.
- **Sizing (`FIXED_K_NATIVE_QUANTITY`):** Target notional is `k * leader native qty * source mark` at an independent follower quote; `source nav=null` is valid.

## Context pointers

| Branch | Read first |
|---|---|
| Architecture, recent Phase-D changes, shared kernel | [README.md](README.md), [CHANGELOG.md](CHANGELOG.md), [packages/copytrade-kernel/README.md](packages/copytrade-kernel/README.md) |
| Capability-scoped gates, sizing policy, challenge rulebooks | [docs/CAPABILITY-SCOPED-READINESS-20261107.md](docs/CAPABILITY-SCOPED-READINESS-20261107.md), [docs/SIZING-POLICY.md](docs/SIZING-POLICY.md), [docs/CHALLENGE-RULEBOOKS.md](docs/CHALLENGE-RULEBOOKS.md) |
| CWSA V3 / Guardian lifecycle, acceptance, release & recovery | [docs/ADR-CANONICAL-5M-CHECKPOINT-GRILL-20261111.md](docs/ADR-CANONICAL-5M-CHECKPOINT-GRILL-20261111.md), [docs/CANONICAL-5M-ACCEPTANCE-20261112.md](docs/CANONICAL-5M-ACCEPTANCE-20261112.md), [docs/CHECKPOINT-INTEGRATION-325.md](docs/CHECKPOINT-INTEGRATION-325.md), [docs/CHECKPOINT-SHADOW-RELEASE-327.md](docs/CHECKPOINT-SHADOW-RELEASE-327.md) |
| Propr read-only evidence (`BrokerGateway`, never derive cash as `equity - unrealized_pnl`) | [docs/NATIVE-CONNECTOR-INTEGRATION.md](docs/NATIVE-CONNECTOR-INTEGRATION.md) |
| Bybit Demo observer (`BYBIT_MODE=read_only`, dedicated Demo credentials) | [docs/BYBIT-DEMO-INTEGRATION.md](docs/BYBIT-DEMO-INTEGRATION.md) |
| Read-only session diagnostics (`prop-production session --path`) | [docs/EXECUTION-SESSION-20261111.md](docs/EXECUTION-SESSION-20261111.md) |
| Local verification (`bash scripts/verify-local.sh app|all`) | [docs/LOCAL-VERIFICATION.md](docs/LOCAL-VERIFICATION.md) |
| Protected deployment (`deploy/install.sh`, `deployment retire-predecessor`, non-rewinding rollback) | [docs/DEPLOYMENT-RUNBOOK.md](docs/DEPLOYMENT-RUNBOOK.md) |
