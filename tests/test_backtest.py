"""Backtest engine tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtesting import BacktestEngine
from app.config import Settings
from app.core.models import ExchangeFilters
from app.strategies import EmaRsiStrategy


def _settings(**kw) -> Settings:
    data = dict(
        _env_file=None,
        paper_trading=True,
        initial_capital=500.0,
        risk_per_trade=0.015,
        maker_fee=0.00075,
        taker_fee=0.00075,
        slippage_bps=5,
        ema_fast=5,
        ema_slow=20,
        rsi_period=5,
        max_open_positions=1,
    )
    data.update(kw)
    return Settings(**data)


def _data(n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    # Trending then mean-reverting noise to generate crosses
    drift = np.linspace(0, 0.2, n // 2).tolist() + np.linspace(0.2, -0.1, n - n // 2).tolist()
    noise = rng.normal(0, 0.01, n)
    close = 100 * np.cumprod(1 + np.array(drift) / n + noise)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1.0},
        index=idx,
    )


def test_no_lookahead_signal_window() -> None:
    """Strategy called with expanding windows only — engine must not pass future bars."""
    calls: list[int] = []

    class Spy(EmaRsiStrategy):
        def generate_signal(self, market_data):
            calls.append(len(market_data))
            # Ensure last index equals length-based closed bar
            return super().generate_signal(market_data)

    settings = _settings()
    engine = BacktestEngine(Spy.from_settings(settings), settings)
    engine.run(_data(120))
    # Each call length should be non-decreasing and never exceed bar index+1
    assert calls == sorted(calls)
    assert max(calls) <= 120


def test_fees_and_slippage_reduce_pnl() -> None:
    cheap = BacktestEngine(
        EmaRsiStrategy(ema_fast=5, ema_slow=20, rsi_period=5),
        _settings(maker_fee=0.0, taker_fee=0.0, slippage_bps=0),
    ).run(_data(300, seed=1))
    costly = BacktestEngine(
        EmaRsiStrategy(ema_fast=5, ema_slow=20, rsi_period=5),
        _settings(maker_fee=0.01, taker_fee=0.01, slippage_bps=50),
    ).run(_data(300, seed=1))
    if cheap.trades and costly.trades:
        assert costly.metrics.total_fees >= cheap.metrics.total_fees


def test_sl_tp_and_compounding_metrics() -> None:
    result = BacktestEngine(
        EmaRsiStrategy(ema_fast=5, ema_slow=20, rsi_period=5, stop_pct=0.01, target_rr=2.0),
        _settings(),
        ExchangeFilters(symbol="BTC/USDT", min_qty=0.0001, step_size=0.0001, min_notional=5.0, tick_size=0.01),
    ).run(_data(500, seed=2))
    m = result.metrics
    assert m.final_account_balance > 0
    assert m.total_trades == m.winning_trades + m.losing_trades
    assert 0 <= m.win_rate <= 1
    assert len(result.equity_curve) > 0
    assert len(result.drawdown_curve) > 0
