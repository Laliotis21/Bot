# Spot Framework — Modular Algorithmic Trading System

Production-oriented **Binance Spot** trading framework under `app/`.

This coexists with the legacy futures bots (`trading_bot.py`, `tsmom_bot.py`,
`funding_bot.py`) and does **not** replace them.

> **WARNING:** This software can place real financial orders.
> Verify configuration before enabling live trading.
>
> **Disclaimer:** Not financial, investment, or tax advice. No profitability is
> guaranteed. Past backtest, Monte Carlo, paper, or live results do not predict
> future performance. You can lose money. Use at your own risk.

## 1. Architecture

Replaceable modules:

| Module | Responsibility |
|---|---|
| `app/strategies` | Signals only (no sizing, no orders) |
| `app/risk` | Position sizing, drawdown CB, daily loss, guards |
| `app/exchanges` | `ExchangeAdapter` + Binance Spot CCXT |
| `app/execution` | Orders, retries, reconciliation, live gate |
| `app/paper` | Simulated fills, identical audit trail |
| `app/backtesting` | Historical sim + walk-forward |
| `app/monte_carlo` | Theoretical + historical-distribution MC |
| `app/accounting` | FIFO ledger + CSV export |
| `app/persistence` | SQLite (Postgres-ready interface) |
| `app/monitoring` | Alerts + WebSocket staleness |

Validation lifecycle (do not skip stages):

Historical Data → Backtesting → Out-of-Sample → Walk-Forward → Monte Carlo →
Paper Trading → Small Live Deployment → Production

## 2. Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## 3. Python version

Requires **Python 3.12+**.

## 4. Dependencies

See `pyproject.toml` / `requirements.txt`:

- ccxt, numpy, pandas, pydantic, pydantic-settings, python-dotenv
- matplotlib, plotly
- pytest (dev)

## 5. Environment configuration

All trading parameters are loaded via Pydantic Settings from `.env`.

Defaults include:

- `INITIAL_CAPITAL=500`
- `RISK_PER_TRADE=0.015` (1.5% of **current** equity)
- `TARGET_RR=2.0`
- `MAX_DRAWDOWN=0.15`
- `MAX_DAILY_LOSS=0.04`
- `MAKER_FEE=0.00075` / `TAKER_FEE=0.00075`
- `PAPER_TRADING=true` (**always default**)

Invalid configs are rejected at startup.

## 6. Binance API configuration

```env
BINANCE_API_KEY=
BINANCE_SECRET_KEY=
EXCHANGE=binance   # or binanceus (separate adapter config)
```

- Never commit secrets.
- Never log credentials.
- Prefer **read-only** keys while developing.
- For trading keys: **disable withdrawal permissions**.

## 7. Paper trading setup

```env
PAPER_TRADING=true
```

```bash
python -m app.cli paper --data path/to/ohlcv.csv
```

Paper mode simulates fills, fees, and slippage and writes the same audit
records as live mode. No real orders are sent.

## 8. Backtesting

```bash
python -m app.cli backtest --symbol BTC/USDT --capital 500
python -m app.cli backtest --data data/ohlcv.csv --start 2024-01-01
```

The engine:

- generates signals only on closed bars (no look-ahead)
- simulates SL/TP, fees, slippage, sizing, compounding, risk limits
- writes `reports/backtest_report.json|html`, equity & drawdown PNGs

**Do not** treat backtest results as proof of future profit.

## 9. Monte Carlo

```bash
python -m app.cli monte-carlo --simulations 1000 --seed 42
python -m app.cli monte-carlo --mode historical --data data/ohlcv.csv
```

- Theoretical mode uses configured win rate / R multiples (illustrative only).
- Historical mode resamples empirical trade R-multiples (not forced 2R).
- Charts: `monte_carlo_paths.png`, `drawdown_distribution.png`,
  `final_balance_distribution.png`

`RANDOM_SEED` makes runs reproducible.

## 10. Walk-forward testing

```bash
python -m app.cli walk-forward --data data/ohlcv.csv
```

Windows are clearly separated: **TRAIN → VALIDATION → TEST**.
Parameters are **not** optimized on the test set.

## 11. Live trading

Default is paper. Live requires:

```env
PAPER_TRADING=false
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISK
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
```

```bash
python -m app.cli live
```

If confirmation is missing, live start is refused.

## 12. Risk controls

- Per-trade risk as % of **current** equity
- Hard drawdown circuit breaker (15% default) — persists peak & trip state,
  cancels entries, alerts, requires **explicit reset**
- UTC daily loss guard (−4%) — blocks new entries for 24h
- Max consecutive losses, max trades/day, max exposure, max positions
- Duplicate position/order protection
- Kill switch, API-error circuit, stale market-data halt

## 13. Tax / audit system

`app/accounting` maintains a transaction ledger with **FIFO** cost basis.

- A BUY is **not** realized PnL.
- CSV export includes the full audit field list.
- CSV is an export/audit layer — **not tax advice** and not a complete filing.

## 14. Database

SQLite by default (`DATABASE_URL=sqlite:///data/trading.db`).

Persists trades, orders, executions, positions, snapshots, risk events,
circuit-breaker state, signals, and daily stats. Interface allows future
PostgreSQL migration.

## 15. Testing

```bash
python -m pytest
```

Coverage includes risk, strategy, backtest (no look-ahead), Monte Carlo,
accounting FIFO, execution failures/partials/duplicates, paper, and CLI gates.

## 16. Troubleshooting

| Issue | Action |
|---|---|
| Config validation error | Check `.env` types/ranges; live needs confirmation phrase |
| Circuit breaker tripped | Inspect risk events; explicit `reset_circuit_breaker()` required |
| Stale data blocks entries | Check WebSocket/monitor; restore market-data feed |
| Exchange errors | Reconcile before retrying orders: `python -m app.cli reconcile` |
| Import shadowing | Do **not** create a `trading_bot/` package (breaks legacy bots) |

## 17. Security

- `.env` is gitignored
- Secrets never logged
- Separate keys for paper/live when possible
- **Disable withdrawals** on API keys
- Default mode is paper trading

## 18. Limitations

- Example EMA/RSI strategy is **not** assumed profitable
- Spot short simulation in backtests is simplified
- WebSocket connector is pluggable; inject exchange-specific streams for prod
- Live CLI initializes/reconciles but does not run an unbounded order loop by
  default (intentional safety scaffold)
- Binance geo-restrictions / HTTP 451 may apply depending on region

## 19. Disclaimer

This software is provided as-is for educational and research use. Trading
cryptocurrencies involves substantial risk of loss. The authors and contributors
are not responsible for financial losses. Distinguish clearly:

1. Theoretical expectancy  
2. Backtest performance  
3. Out-of-sample / walk-forward performance  
4. Paper trading performance  
5. Live performance  

None of the above guarantees the next.
