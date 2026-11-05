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

## Reproduce

Python 3.13 or 3.14 and uv 0.12.15:

```bash
uv sync --frozen --group dev
uv run --frozen prop-production status
uv run --frozen prop-production smoke
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen mypy
uv build --no-sources
```

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
