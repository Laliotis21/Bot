# Spot framework deployment notes

## Docker

```bash
docker build -t spot-trading-framework .
docker run --rm -v "$PWD/reports:/app/reports" spot-trading-framework
```

Override command for Monte Carlo:

```bash
docker run --rm -v "$PWD/reports:/app/reports" spot-trading-framework \
  python -m app.cli monte-carlo --simulations 200 --seed 42
```

**Never** pass live API keys into a container unless you intend to trade.
Default `PAPER_TRADING=true`.

## Security checklist

1. Disable withdrawal permissions on API keys.
2. Use IP allowlists when available.
3. Separate keys for research vs production.
4. Confirm `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISK` only when ready.
5. Run walk-forward + Monte Carlo + paper before any live capital.

## Persistence

Mount `data/` for SQLite survival across restarts:

```bash
docker run --rm -v "$PWD/data:/app/data" -v "$PWD/reports:/app/reports" \
  -e PAPER_TRADING=true spot-trading-framework \
  python -m app.cli backtest
```
