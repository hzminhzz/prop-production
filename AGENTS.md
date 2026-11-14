# prop-production execution contract

Default mode is **SHADOW / NO_TRADE**. Never commit credentials, wallet identities, private account responses, datasets, or signing secrets. Mode flags, passing tests, or service restarts never grant live trading authority; keep `IMPLEMENTED`, `ENGINEERING_VERIFIED`, `DEPLOYED`, `RUNNING_OBSERVED`, `SCIENTIFICALLY_QUALIFIED`, and `TRADING_AUTHORIZED` strictly separate.

## Orientation & scope

- **Branch & worktrees:** Default branch is `feat/account-guardian-program` (recheck before pushing). Preserve concurrent sessions and use isolated worktrees when dirty. Read `README.md`, the owning module, and behavioral tests before editing.
- **Capability-scoped gates:** Read [capability-scoped readiness](docs/CAPABILITY-SCOPED-READINESS-20261107.md). Source `OBSERVE` and read-only `PAPER` collection are decoupled from protected trading attestations (`read_only_collection=EVIDENCE_COLLECTED` preserves `BLOCKED_PAPER_ADMISSION` without executable follower economics). `DEMO`/`LIVE` require account-bound reconciliation, equity, signed release, risk checks, and explicit authorization.
- **Durable navigation:** Use OptMem for cross-session context, then verify directly against Git, SQLite, and OS state. Execute directly unless delegation is explicitly requested.

## Authority boundaries

- **Domain owners:** Research metadata belongs to `copytrade-pm` SQLite; provider protocol belongs to `trading-connector`; Guardian account finance and committed intents belong to this app alone. Never place network I/O inside a SQL transaction or let a connector journal replace a Guardian intent.
- **Propr & Planner:** Consume read-only Propr evidence strictly via `connectors/propr/native_evidence.py` and `BrokerGateway`, preserving native identity, timestamps, and `RECONCILIATION_UNCERTAIN`. Never derive cash as `equity - unrealized_pnl` or treat REST snapshots as coherent history. Planner never supplies equity, permits, or credentials.
- **Bybit Demo:** Independently authenticated read-only observer (`BYBIT_MODE=read_only`, pinned `BybitClient`, explicit Demo UID/key/endpoint). Never fall back to `BYBIT_PROP_*` or mainnet/testnet credentials. See [Bybit Demo integration](docs/BYBIT-DEMO-INTEGRATION.md).
- **Execution Session diagnostics:** `prop-production session --path` is a strictly read-only, unverified-claims diagnostic—never a broker permit, financial attestation, or HALT-clear signal. Native Propr order authority is currently unavailable. See [Execution Session v1](docs/EXECUTION-SESSION-20261111.md).

## Checkpoint lifecycle & sizing

- **Explicit transitions only:** Keep typed predecessor/trigger/guard/next-state/durable-effect contracts; never build implicit FSMs over `is_active`, `ready`, age, missing rows, or service health. Existing broker order journals remain the only protected financial truth: shared CWSA continuity does not reset on single-mandate change, and HALTs/unknown orders cannot clear from source health. See [CWSA/Guardian design ADR](docs/ADR-CANONICAL-5M-CHECKPOINT-GRILL-20261111.md), [adversarial acceptance map](docs/CANONICAL-5M-ACCEPTANCE-20261112.md), [integration evidence](docs/CHECKPOINT-INTEGRATION-325.md), and [nonactivating release/recovery contract](docs/CHECKPOINT-SHADOW-RELEASE-327.md).
- **Default sizing (`FIXED_K_NATIVE_QUANTITY`):** Follower target notional is `k * leader native qty * source mark`, sized at an independent follower quote and Guardian risk-checked. `source nav=null` is valid and MUST NOT fail on `SOURCE_NAV` or trigger parent/subaccount/Vike NAV reconstruction. Reserve `SOURCE_NAV` checks strictly for explicit `ALLOCATION_OVER_LIVE_NAV` policies. Enforce signed releases, causal clocks, risk/drawdown/margin/HALT gates, and note gap/slippage around pod stops. See [sizing policy](docs/SIZING-POLICY.md).

## Verification & deployment

- **Pins & local gates:** `src/prop_production/compatibility.json` is the sole companion/schema pin authority. Before merge, run commit-bound local verification with both pinned companions per [local verification](docs/LOCAL-VERIFICATION.md).
- **Invariants under test:** Exercise `CLI`, `TargetBatchProposal -> DecisionReceipt -> durable intent -> BrokerGateway` with exact decimal accounting. Commit reservations and intent before dispatch; unknown outcome is not failure; persist `HALT` across restart and midnight; missing evidence is `BLOCKED`. Outside Judge must never import Guardian risk or finance as its oracle.
- **Protected installation:** Follow [deployment runbook](docs/DEPLOYMENT-RUNBOOK.md). Bundle app, connector, and units together, use named rollback without rewinding journals or the trust floor, and verify live process/release identity rather than symlink presence. Keep `IMPLEMENTED`, `ENGINEERING_VERIFIED`, `DEPLOYED`, `RUNNING_OBSERVED`, `SCIENTIFICALLY_QUALIFIED`, and `TRADING_AUTHORIZED` distinct when closing tickets.
