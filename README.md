# prop-production

Account-guarded copytrading application. Default: **SHADOW / DISARMED / NO_TRADE**.
No live broker mutation route is installed or authorized.

## Ownership

- `copytrade-pm`: research, SQLite metadata authority and immutable portfolio exports.
- `trading-connector`: reusable provider transports and exact account/order evidence.
- `prop-production`: Planner, Account Guardian, financial journal, risk supervision,
  reconciliation, watchdog and independently checked bounded-safety evidence.

The Planner has no broker credentials. Only a separately isolated Guardian could
hold financial mutation authority; this release provides zero-capital interfaces.

## Reproduce and verify locally

GitHub Actions is optional and **not required** for this private VPS program.
Python 3.13/3.14 and uv 0.12.15 are used. From a clean reviewed commit,
verify the full application + two companion repositories:

```bash
export PROP_RESEARCH_ROOT=/home/quant/.devspace/worktrees/copytrade-pm-dade3502
export PROP_CONNECTOR_ROOT=/home/quant/.devspace/worktrees/trading-connector-829a9992
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
