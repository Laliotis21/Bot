"""Backtest performance metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.core.models import TradeRecord


@dataclass
class BacktestMetrics:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    total_return: float = 0.0
    cagr: float = 0.0
    maximum_drawdown: float = 0.0
    average_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    average_trade_duration: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    final_account_balance: float = 0.0
    monthly_returns: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: getattr(self, k) for k in self.__dataclass_fields__ if k != "monthly_returns"},
            "monthly_returns": self.monthly_returns,
        }


def _max_streak(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        if f:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def compute_metrics(
    trades: list[TradeRecord],
    equity_curve: pd.Series,
    *,
    initial_capital: float,
    periods_per_year: float = 365.0,
) -> BacktestMetrics:
    """Compute standard backtest statistics from trades and equity curve."""
    m = BacktestMetrics(final_account_balance=float(equity_curve.iloc[-1]) if len(equity_curve) else initial_capital)
    if not len(equity_curve):
        return m

    m.total_return = (m.final_account_balance / initial_capital) - 1.0
    m.total_trades = len(trades)
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    m.winning_trades = len(wins)
    m.losing_trades = len(losses)
    m.win_rate = m.winning_trades / m.total_trades if m.total_trades else 0.0
    m.average_win = float(np.mean(wins)) if wins else 0.0
    m.average_loss = float(np.mean(losses)) if losses else 0.0
    gross_win = float(sum(wins))
    gross_loss = float(abs(sum(losses)))
    m.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    m.expectancy = float(np.mean([t.net_pnl for t in trades])) if trades else 0.0
    m.total_fees = float(sum(t.fees for t in trades))
    m.total_slippage = float(sum(t.slippage for t in trades))
    m.best_trade = float(max((t.net_pnl for t in trades), default=0.0))
    m.worst_trade = float(min((t.net_pnl for t in trades), default=0.0))
    m.average_trade_duration = float(np.mean([t.duration_seconds for t in trades])) if trades else 0.0
    m.max_consecutive_wins = _max_streak([t.net_pnl > 0 for t in trades])
    m.max_consecutive_losses = _max_streak([t.net_pnl <= 0 for t in trades])

    # Drawdowns
    peak = equity_curve.cummax()
    dd = (peak - equity_curve) / peak.replace(0, np.nan)
    dd = dd.fillna(0.0)
    m.maximum_drawdown = float(dd.max())
    m.average_drawdown = float(dd.mean())

    # Returns-based ratios
    rets = equity_curve.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        m.sharpe_ratio = float(np.sqrt(periods_per_year) * rets.mean() / rets.std())
        downside = rets[rets < 0]
        downside_std = float(downside.std()) if len(downside) else 0.0
        m.sortino_ratio = (
            float(np.sqrt(periods_per_year) * rets.mean() / downside_std) if downside_std > 0 else 0.0
        )
    if m.maximum_drawdown > 0:
        m.calmar_ratio = float(m.total_return / m.maximum_drawdown)

    # CAGR
    if isinstance(equity_curve.index, pd.DatetimeIndex) and len(equity_curve) >= 2:
        days = max((equity_curve.index[-1] - equity_curve.index[0]).total_seconds() / 86400.0, 1.0)
        years = days / 365.25
        if years > 0 and m.final_account_balance > 0 and initial_capital > 0:
            m.cagr = float((m.final_account_balance / initial_capital) ** (1 / years) - 1.0)

    # Monthly stats
    if isinstance(equity_curve.index, pd.DatetimeIndex):
        monthly = equity_curve.resample("ME").last().pct_change().dropna()
        m.monthly_returns = {idx.strftime("%Y-%m"): float(val) for idx, val in monthly.items()}

    return m


def equity_and_drawdown_frames(equity_curve: pd.Series) -> tuple[pd.Series, pd.Series]:
    peak = equity_curve.cummax()
    drawdown = (peak - equity_curve) / peak.replace(0, np.nan)
    return equity_curve, drawdown.fillna(0.0)
