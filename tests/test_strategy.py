"""Strategy unit tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core import SignalType
from app.strategies import EmaRsiStrategy


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.ones(n),
        },
        index=idx,
    )


def test_buy_signal_on_cross_up() -> None:
    # Slow downtrend then sharp rally to force EMA cross up
    closes = [100 - i * 0.5 for i in range(60)] + [70 + i for i in range(30)]
    strat = EmaRsiStrategy(ema_fast=5, ema_slow=20, rsi_period=5, rsi_overbought=95, stop_pct=0.02)
    sig = strat.generate_signal(_ohlcv(closes))
    assert sig is not None
    assert sig.signal_type in {SignalType.BUY, SignalType.NONE, SignalType.SELL}
    # Walk until we observe a BUY on expanding window (no look-ahead in strategy itself)
    found_buy = False
    df = _ohlcv(closes)
    for i in range(25, len(df)):
        s = strat.generate_signal(df.iloc[: i + 1])
        if s and s.signal_type == SignalType.BUY:
            found_buy = True
            assert s.entry_price is not None
            assert s.stop_loss is not None
            assert s.take_profit is not None
            assert s.stop_loss < s.entry_price
            assert s.take_profit > s.entry_price
            break
    assert found_buy


def test_sell_signal_on_cross_down() -> None:
    closes = [50 + i * 0.5 for i in range(60)] + [80 - i for i in range(30)]
    strat = EmaRsiStrategy(ema_fast=5, ema_slow=20, rsi_period=5, rsi_oversold=5, stop_pct=0.02)
    df = _ohlcv(closes)
    found_sell = False
    for i in range(25, len(df)):
        s = strat.generate_signal(df.iloc[: i + 1])
        if s and s.signal_type == SignalType.SELL:
            found_sell = True
            assert s.stop_loss is not None and s.entry_price is not None
            assert s.stop_loss > s.entry_price
            break
    assert found_sell


def test_no_signal_insufficient_data() -> None:
    strat = EmaRsiStrategy(ema_fast=20, ema_slow=50)
    sig = strat.generate_signal(_ohlcv([100.0] * 10))
    assert sig is not None
    assert sig.signal_type == SignalType.NONE


def test_invalid_data_raises() -> None:
    strat = EmaRsiStrategy()
    with pytest.raises(ValueError):
        strat.generate_signal(pd.DataFrame({"open": [1, 2, 3]}))
