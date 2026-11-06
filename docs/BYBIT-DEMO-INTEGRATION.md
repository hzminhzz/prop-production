# Bybit Demo integration — read-only SHADOW evidence

Scope: independent Bybit Demo account observer. It does not alter Propr, the synthetic Guardian, Planner, journals, or trading authorization.

## Provider and credentials

The pinned trading-connector implementation (b28aa8522c51830a4b389a61b20ceb539b7a89ca) owns all V5/CCXT protocol calls through BybitClient. The application does not create a second Bybit API client.

Use only the dedicated existing Demo file at /home/quant/.config/trading-connector/bybit-demo.env, installed as root-only /etc/prop-production/bybit.env. Required exact environment is BYBIT_ENVIRONMENT=demo and BYBIT_MODE=read_only. Never substitute BYBIT_PROP_* mainnet/testnet or generic credentials. The connector checks the authenticated UID/key, unified account and exact private endpoint before reading.

Systemd runs prop-production-bybit-observer.service as prop-bybit. The private account tape is /var/lib/prop-production/bybit/observations.jsonl. The heartbeat is /var/lib/prop-production/health/bybit/heartbeat.json. The account UID/key and unfiltered response are not logged or committed.

## Financial and history semantics

Bybit V5 totalEquity is provider account-wide USD equity. totalAvailableBalance is available USD buying power, not cash or wallet balance. Exact Decimal values are preserved.

The existing connector reads bounded supported linear USDT/USDC positions and open orders, with strict pagination and explicit rejection of unsupported active categories, hedge mode and conditional orders.

inspect_bybit_demo_readonly() maps these into a non-authorizing BrokerSnapshot. Cash, unrealized PnL, initial and maintenance margins, provider cutoff and verified private stream continuity remain unavailable. history_complete, connected, financial_complete and opening readiness remain false. An empty bounded REST inventory is not historical proof that the account is flat.

ReadOnlyGateway.submit() and cancel() deny mutation. The Guardian SQLite journal owns all intents. The adapter never creates a trading intent or accesses the broker's account credentials.

## Verification and operation

Single read-only smoke: run the app's bybit_observer module with --once, a private PROP_BYBIT_STATE directory, and only the root-protected dedicated Demo credentials.

Normal service: systemctl status prop-production-bybit-observer.service, plus the private tape and role-owned heartbeat. The independent watchdog exposes a distinct bybit_status, which is never trading permission.

Run app tests tests/test_bybit_gateway.py and tests/test_bybit_observer.py, then the complete commit-pinned scripts/verify-local.sh all gate (including Bybit connector regression). Tests and observations must send zero account mutations.

The connector's separate demo_pm_canary.py already contains an explicitly opted-in small Demo order-and-close diagnostic. It is NOT wired into prop-production and must not be mistaken for Guardian execution qualification.

## Unqualified

Coherent replay history and cutoff, all fills/fees/funding, account-specific prop challenge terms, actual first-known wallet signals, elapsed wait, follower PnL, four-week prospective paper and G0-G4 authorization are not established. Keep SHADOW / NO_TRADE.

Official Bybit documentation:
- https://bybit-exchange.github.io/docs/v5/demo
- https://bybit-exchange.github.io/docs/v5/account/wallet-balance
