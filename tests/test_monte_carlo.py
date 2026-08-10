"""Monte Carlo tests."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.enums import Side
from app.core.models import TradeRecord
from app.monte_carlo import MonteCarloConfig, MonteCarloEngine


def test_deterministic_seed() -> None:
    cfg = MonteCarloConfig(simulations=50, trades_per_sim=40, random_seed=42)
    a = MonteCarloEngine(cfg).run_theoretical()
    b = MonteCarloEngine(cfg).run_theoretical()
    assert (a.final_balances == b.final_balances).all()
    assert a.median_final_balance == b.median_final_balance


def test_simulation_count_and_percentiles() -> None:
    cfg = MonteCarloConfig(simulations=100, trades_per_sim=50, random_seed=1)
    r = MonteCarloEngine(cfg).run_theoretical()
    assert len(r.final_balances) == 100
    assert r.equity_paths.shape == (100, 51)
    assert r.p5_final_balance <= r.median_final_balance <= r.p95_final_balance
    assert 0 <= r.probability_of_ruin <= 1
    assert "25pct" in r.prob_drawdown


def test_historical_mode_uses_trade_distribution() -> None:
    now = datetime.now(timezone.utc)
    trades = [
        TradeRecord(
            position_id="p1",
            symbol="BTC/USDT",
            side=Side.BUY,
            quantity=1,
            entry_price=100,
            exit_price=104,
            stop_loss=98,
            take_profit=104,
            gross_pnl=4,
            fees=0.1,
            slippage=0.05,
            net_pnl=3.85,
            r_multiple=1.9,
            opened_at=now,
            closed_at=now,
        ),
        TradeRecord(
            position_id="p2",
            symbol="BTC/USDT",
            side=Side.BUY,
            quantity=1,
            entry_price=100,
            exit_price=98,
            stop_loss=98,
            take_profit=104,
            gross_pnl=-2,
            fees=0.1,
            slippage=0.05,
            net_pnl=-2.15,
            r_multiple=-1.1,
            opened_at=now,
            closed_at=now,
        ),
    ]
    cfg = MonteCarloConfig(simulations=30, trades_per_sim=20, random_seed=7)
    r = MonteCarloEngine(cfg).run_historical(trades)
    assert len(r.final_balances) == 30
    assert r.mode == "historical"
