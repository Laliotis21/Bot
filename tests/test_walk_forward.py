"""Walk-forward separation tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtesting import WalkForwardTester
from app.config import Settings
from app.strategies import EmaRsiStrategy


def test_walk_forward_separates_windows() -> None:
    settings = Settings(
        _env_file=None,
        paper_trading=True,
        initial_capital=500,
        ema_fast=5,
        ema_slow=15,
        rsi_period=5,
    )
    n = 700
    idx = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.002, n))
    df = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1},
        index=idx,
    )
    report = WalkForwardTester(
        EmaRsiStrategy.from_settings(settings),
        settings,
        train_bars=300,
        validation_bars=100,
        test_bars=100,
        step_bars=150,
    ).run(df)
    assert report.folds
    f = report.folds[0]
    assert f.train_end < f.validation_start
    assert f.validation_end < f.test_start
    assert "avg_total_return" in report.in_sample
    assert "avg_total_return" in report.out_of_sample
    assert "avg_total_return" in report.walk_forward
