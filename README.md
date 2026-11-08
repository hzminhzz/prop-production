# prop-production

Account-guarded copytrading application. Default: **SHADOW / DISARMED / NO_TRADE**.
No live broker mutation route is installed or authorized.

[Capability-scoped readiness](docs/CAPABILITY-SCOPED-READINESS-20261107.md) separates selected-source OBSERVE, read-only PAPER collection, independently qualified follower economics, DEMO and LIVE. Neither an unrelated missing research proof nor elapsed 72-hour soak is a universal read-only observer gate. Source collection never grants broker authority.

## Ownership

- `copytrade-pm`: research, SQLite metadata authority and immutable portfolio exports.
- `trading-connector`: reusable provider transports and exact account/order evidence.
- `prop-production`: Planner, Account Guardian, financial journal, risk supervision,
  reconciliation, watchdog and independently checked bounded-safety evidence.

The Planner has no broker credentials. Only a separately isolated Guardian could
hold financial mutation authority; this release provides zero-capital interfaces.

**Sizing policy:** risk-budgeted fixed-k mirroring is the approved default for new design work; leader whole-account NAV is *not* an admission prerequisite for `FIXED_K_NATIVE_QUANTITY`. The signed policy requires an explicit k and complete selected account-position scope, while Guardian still controls follower equity, risk, limits, and execution. Existing `ALLOCATION_OVER_LIVE_NAV` releases retain strict NAV requirements. See [the sizing policy](docs/SIZING-POLICY.md).

## Official V4 position-source SHADOW integration

The additive, externally authenticated source export, per-transition T+300
eligibility projection and Guardian-owned SHADOW risk preview are documented in
[the V4 integration runbook](docs/V4-SHADOW-INTEGRATION-20261108.md).
They do not replace the installed synthetic Planner or authorize broker orders.
The protected cross-user transfer and signed prospective release still require
independent verification before a deployable integrated service exists.

## Native Propr evidence integration

The optional provider connector exposes `propr_native_evidence_v1`. `BrokerGateway` consumes the versioned read-only evidence, including exact Decimal provider balance, equity formula provenance, buying power, native account/attempt identity, inventory and explicit uncertainty reasons. Propr `availableBalance` is buying power, **not** trusted Guardian cash. No `equity - unrealized` cash proxy is created. Offset REST pages and WebSocket events do not establish a coherent provider fill cutoff, so Propr returns `RECONCILIATION_UNCERTAIN`, `history_complete=False` and **NO_TRADE** even when the diagnostic inventory reads successfully. The Guardian alone owns SQLite financial state, durable intents and risk authorization. Optional ML4T 0.1.2 remains a separate consumer; no ML4T runtime is imported here. See [native integration notes](docs/NATIVE-CONNECTOR-INTEGRATION.md).

## Bybit Demo read-only integration

A separate `prop-production-bybit-observer.service` consumes the pinned connector's authenticated Bybit Demo account, position and order readers. It uses the dedicated Demo key/UID, not generic Bybit credentials. Its private observations retain exact USD equity, available buying power, position/order diagnostics, identity verification and explicit UNKNOWN cash/history. A separate watchdog field reports Bybit observer health; this does not authorize trading or establish a market/source feed. The existing Propr observer and synthetic Guardian remain independent. See [Bybit Demo integration](docs/BYBIT-DEMO-INTEGRATION.md).

## Reproduce and verify locally

GitHub Actions is optional and **not required** for this private VPS program.
Use Python 3.13/3.14 and record the actual uv version. The reviewed default
branch is `feat/account-guardian-program`; verify it live. Companion pins and
schemas come from [compatibility.json](src/prop_production/compatibility.json).
From a clean reviewed commit, verify the application and both companions:

```bash
PYTHONPATH=src python3 -m prop_production.compatibility
export PROP_RESEARCH_ROOT=/absolute/path/to/clean/pinned-copytrade-pm
export PROP_CONNECTOR_ROOT=/absolute/path/to/clean/pinned-trading-connector
bash scripts/verify-local.sh all
```

Application-only checks: `bash scripts/verify-local.sh app`. These are
**not** sufficient for cross-repository acceptance. See
[local verification requirements](docs/LOCAL-VERIFICATION.md) for pin/clean
checks, evidence logging and the fail-closed limits of passing tests.

Invalid modes exit 2. Smoke data are synthetic and carry no profitability claim.

## Evidence vocabulary

IMPLEMENTED, ENGINEERING_VERIFIED, DEPLOYED, RUNNING_OBSERVED,
SCIENTIFICALLY_QUALIFIED and TRADING_AUTHORIZED are separate claims. Tests cannot
grant financial permission. A signature authenticates bytes; it cannot establish
profitable follower execution. Bounded safety requires explicit stress limits,
and cannot guarantee survival through arbitrary gaps or provider outages.

Private wallet identities, raw fills, account snapshots, databases, credentials
and signing keys stay outside Git. Public fixtures use invented identities.
The parent contract is [issue #1](https://github.com/hzminhzz/prop-production/issues/1);
each implementation ticket retains its own acceptance evidence.

## Deployed release

Read `/opt/prop-production/current/COMMIT`, verify the complete immutable bundle,
and match actual service PIDs and fresh role evidence to it. The pointer alone is
not proof of loaded code. Choose a named, verified, schema-compatible rollback
bundle. Follow [deployment and recovery](docs/DEPLOYMENT-RUNBOOK.md); root release
authority is required. Journals and the trust floor remain outside code releases.

GitHub holds reviewed source; private SQLite holds research metadata; original
receipts hold source evidence; Guardian journals hold protected finance; verified
deployment metadata plus OS evidence establish running state. OptMem supplies
durable navigation and never overrides these authorities. These GitHub repositories
are public; private operating data must remain outside Git.
