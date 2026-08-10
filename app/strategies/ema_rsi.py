"""EMA crossover + RSI filter example strategy.

This strategy is an example only and is NOT assumed to be profitable.
"""

from __future__ import annotations

import pandas as pd

from app.config.settings import Settings
from app.core.enums import SignalType
from app.core.models import Signal, utc_now
from app.strategies.base import Strategy
from app.utils.time import ensure_utc, ms_to_utc


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


class EmaRsiStrategy(Strategy):
    """BUY on fast EMA crossing above slow EMA with RSI not overbought.

    SELL on fast EMA crossing below slow EMA with RSI not oversold.
    Produces entry/stop/take-profit hints; never sizes or places orders.
    """

    name = "ema_rsi"

    def __init__(
        self,
        *,
        symbol: str = "BTC/USDT",
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        target_rr: float = 2.0,
        stop_atr_mult: float = 1.0,
        stop_pct: float = 0.02,
    ) -> None:
        if ema_slow <= ema_fast:
            raise ValueError("ema_slow must be > ema_fast")
        self.symbol = symbol
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.target_rr = target_rr
        self.stop_atr_mult = stop_atr_mult
        self.stop_pct = stop_pct

    @classmethod
    def from_settings(cls, settings: Settings) -> EmaRsiStrategy:
        return cls(
            symbol=settings.symbol,
            ema_fast=settings.ema_fast,
            ema_slow=settings.ema_slow,
            rsi_period=settings.rsi_period,
            rsi_oversold=settings.rsi_oversold,
            rsi_overbought=settings.rsi_overbought,
            target_rr=settings.target_rr,
        )

    def generate_signal(self, market_data: pd.DataFrame) -> Signal | None:
        if market_data is None or market_data.empty:
            return None
        required = {"close"}
        if not required.issubset(set(market_data.columns)):
            raise ValueError("market_data must contain at least a 'close' column")

        df = market_data.copy()
        if len(df) < self.ema_slow + 2:
            return Signal(signal_type=SignalType.NONE, symbol=self.symbol)

        closes = df["close"].astype(float)
        ema_fast = compute_ema(closes, self.ema_fast)
        ema_slow = compute_ema(closes, self.ema_slow)
        rsi = compute_rsi(closes, self.rsi_period)

        # Use last CLOSED bar only (caller must exclude incomplete candle)
        i = len(df) - 1
        if i < 1:
            return Signal(signal_type=SignalType.NONE, symbol=self.symbol)

        prev_fast, curr_fast = float(ema_fast.iloc[i - 1]), float(ema_fast.iloc[i])
        prev_slow, curr_slow = float(ema_slow.iloc[i - 1]), float(ema_slow.iloc[i])
        curr_rsi = float(rsi.iloc[i])
        entry = float(closes.iloc[i])

        ts = df.index[i] if isinstance(df.index, pd.DatetimeIndex) else None
        if "timestamp" in df.columns:
            raw = df["timestamp"].iloc[i]
            if isinstance(raw, (int, float)):
                ts = ms_to_utc(int(raw)) if raw > 10_000_000_000 else ensure_utc(
                    pd.to_datetime(raw, unit="s", utc=True).to_pydatetime()
                )
            else:
                ts = ensure_utc(pd.to_datetime(raw, utc=True).to_pydatetime())

        cross_up = prev_fast <= prev_slow and curr_fast > curr_slow
        cross_down = prev_fast >= prev_slow and curr_fast < curr_slow

        stop_dist = entry * self.stop_pct
        if "high" in df.columns and "low" in df.columns and len(df) >= 15:
            tr = (df["high"] - df["low"]).astype(float).iloc[i - 14 : i].mean()
            if tr and tr > 0:
                stop_dist = float(tr) * self.stop_atr_mult

        meta = {
            "ema_fast": curr_fast,
            "ema_slow": curr_slow,
            "rsi": curr_rsi,
        }

        stamp = ts or utc_now()

        if cross_up and curr_rsi < self.rsi_overbought:
            stop = entry - stop_dist
            tp = entry + stop_dist * self.target_rr
            return Signal(
                signal_type=SignalType.BUY,
                symbol=self.symbol,
                timestamp=stamp,
                entry_price=entry,
                stop_loss=stop,
                take_profit=tp,
                metadata=meta,
            )

        if cross_down and curr_rsi > self.rsi_oversold:
            stop = entry + stop_dist
            tp = entry - stop_dist * self.target_rr
            return Signal(
                signal_type=SignalType.SELL,
                symbol=self.symbol,
                timestamp=stamp,
                entry_price=entry,
                stop_loss=stop,
                take_profit=tp,
                metadata=meta,
            )

        return Signal(
            signal_type=SignalType.NONE,
            symbol=self.symbol,
            timestamp=stamp,
            metadata=meta,
        )
