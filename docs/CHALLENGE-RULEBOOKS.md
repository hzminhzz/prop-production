# Canonical Challenge Firm Rulebooks

`prop_production` ships an exact, self-contained catalog of prop-firm challenge `Rulebook` specifications in [`src/prop_production/prop_firms.py`](../src/prop_production/prop_firms.py) (sourced from `trading-ops/prop_firms_rules.json`) for portfolio research, backtesting, and SHADOW verification.

## Agent & Research Discovery

### Python API
```python
from prop_production.finance import Rulebook
from prop_production.prop_firms import get_challenge_spec, list_challenge_specs

# List all 36 canonical keys (or filter by firm_slug)
keys = Rulebook.list_challenges()
hyro_keys = Rulebook.list_challenges("hyrotrader")

# Materialize an exact Rulebook ($5,000 base, or scale=2 for $10,000)
rules = Rulebook.from_challenge("hyrotrader:two_step:phase_1")
rules_10k = Rulebook.from_challenge("hyrotrader:two_step:phase_1", scale=2)

# Inspect extra non-Guardian constraints (consistency, prohibited venues/pairs, ambiguities)
spec = get_challenge_spec("hyrotrader:two_step:phase_1")
```

### CLI Discovery (Read-Only JSON)
```bash
uv run prop-production rulebooks
uv run prop-production rulebooks --firm hyrotrader
uv run prop-production rulebooks --rulebook-key hyrotrader:two_step:phase_1 --scale 2
```

## Supported Firms & Basis Semantics

- **`daily_basis`**:
  - `EQUITY`: 00:00 UTC equity baseline (`hyrotrader`, `vanta-trading` Grow/Pro/Instant)
  - `BALANCE`: 00:00 UTC cash/wallet-balance baseline (`northbook`, `vanta-trading` Classic, `crypto-fund-trader`, `hypernova`, `propr`)
  - `MAX_BALANCE_EQUITY`: `max(cash, equity)` synthetic default
- **`trailing_basis`**:
  - `STATIC`: fixed floor at `initial - trailing_loss` (never trails upward)
  - `INTRADAY_EQUITY`: trails intraday high-water mark (`propr:classic_2step:*`)
  - `EOD_EQUITY`: trails end-of-day UTC high-water mark (`vanta-trading` Grow/Pro/Instant)
- **Funded phase `target`**: `"0"` when a firm has no profit target in the funded phase.

| Firm (`firm_slug`) | Keys (`<firm>:<program>:<phase>`) | Initial | Target | Daily Loss (`basis`) | Max Drawdown (`trailing_basis`) | Extra Constraints |
|---|---|---:|---:|---|---|---|
| `northbook` | `northbook:1step_{base,classic,prime}:{phase_1,funded}` | 5000 | 450 / 500 / 0 | 150 (`BALANCE`) | 150 / 300 / 400 (`STATIC`) | Hyperliquid-priced USDC sim |
| `vanta-trading` | `vanta-trading:{classic_1step_3pct,classic_1step_5pct}:phase_1`, `classic_scaled:funded`, `{grow,pro,instant_funded_5pct,instant_funded_8pct}:funded` | 5000 | 500 / 300 / 0 | 150 or 250 (`BALANCE` / `EQUITY`) | 250 or 400 (`STATIC` / `EOD_EQUITY`) | Grow: 20% consistency, min 90d |
| `hyrotrader` | `hyrotrader:two_step:{phase_1,phase_2,funded}` | 5000 | 500 / 250 / 0 | 250 (`EQUITY`) | 500 (`STATIC`) | USDT perps only; 40% eval consistency; no EUR/USD; low-cap < $100M mcap <= 5% |
| `crypto-fund-trader` | `crypto-fund-trader:two_phase:{phase_1,phase_2,funded}` | 5000 | 400 / 250 / 0 | 250 (`BALANCE`) | 500 (`STATIC`) | Bybit USDT perps only; 00:05 UTC reset |
| `hypernova` | `hypernova:1step_{tight,low,medium,high}:{phase_1,funded}` | 5000 | 450 / 500 / 0 | 150 / 200 / 250 (`BALANCE`) | 150 / 300 / 350 / 400 (`STATIC`) | Hyperliquid-priced internal engine |
| `propr` | `propr:{classic_1step,turbo_1step,pro_1step}:{phase_1,funded}`, `propr:classic_2step:{phase_1,phase_2,funded}` | 5000 | 450-600 / 250-500 / 0 | 150 or 250 (`BALANCE`) | 150-300 (`STATIC`) / 400 (`INTRADAY_EQUITY`) | 2-Step trailing locks at initial |
