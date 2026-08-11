"""Historical backtesting engine with no look-ahead bias."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.config.settings import Settings
from app.core.enums import Side, SignalType
from app.core.models import ExchangeFilters, TradeRecord, new_id
from app.risk.manager import RiskManager
from app.strategies.base import Strategy
from app.backtesting.metrics import BacktestMetrics, compute_metrics, equity_and_drawdown_frames
from app.utils.logging import get_logger
from app.utils.precision import round_price, round_quantity

logger = get_logger("trading.backtest")


@dataclass
class OpenBacktestPosition:
    position_id: str
    side: Side
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_time: datetime
    entry_fees: float
    entry_slippage: float


@dataclass
class BacktestResult:
    metrics: BacktestMetrics
    trades: list[TradeRecord]
    equity_curve: pd.Series
    drawdown_curve: pd.Series
    signals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.to_dict(),
            "trades": [t.model_dump(mode="json") for t in self.trades],
            "equity_curve": {
                str(k): float(v) for k, v in self.equity_curve.items()
            },
        }


class BacktestEngine:
    """Simulate strategy + risk + fees/slippage on historical OHLCV.

    Look-ahead control:
    - Signal at bar ``i`` is generated from ``data.iloc[: i + 1]`` (closed bar i).
    - Entries fill on bar ``i`` close (conservative: after signal confirmation).
    - SL/TP checked on subsequent bars using high/low without using future bars
      for signal generation.
    """

    def __init__(
        self,
        strategy: Strategy,
        settings: Settings,
        filters: ExchangeFilters | None = None,
    ) -> None:
        self.strategy = strategy
        self.settings = settings
        self.filters = filters or ExchangeFilters(
            symbol=settings.symbol,
            min_qty=0.0001,
            step_size=0.0001,
            min_notional=10.0,
            tick_size=0.01,
            price_precision=2,
            quantity_precision=4,
        )

    def run(self, data: pd.DataFrame) -> BacktestResult:
        df = self._normalize(data)
        risk = RiskManager(self.settings, equity=self.settings.initial_capital)
        cash = float(self.settings.initial_capital)
        position: OpenBacktestPosition | None = None
        trades: list[TradeRecord] = []
        equity_points: list[tuple[datetime, float]] = []
        signals: list[dict[str, Any]] = []

        fee_rate = self.settings.effective_taker_fee
        slip = self.settings.slippage_rate

        for i in range(len(df)):
            row = df.iloc[i]
            ts = df.index[i].to_pydatetime() if hasattr(df.index[i], "to_pydatetime") else df.index[i]
            window = df.iloc[: i + 1]  # no future bars

            # Manage open position on this bar (SL/TP using high/low)
            if position is not None:
                exit_price, reason = self._check_exit(position, row)
                if exit_price is not None:
                    trade, cash = self._close(position, exit_price, ts, cash, fee_rate, slip, reason)
                    trades.append(trade)
                    risk.unregister_position(self.settings.symbol, position.quantity * position.entry_price)
                    risk.record_realized_trade(trade.net_pnl, notional_closed=position.quantity * position.entry_price)
                    position = None

            # Mark-to-market equity
            equity = cash
            if position is not None:
                mark = float(row["close"])
                if position.side == Side.BUY:
                    equity = cash + position.quantity * mark
                else:
                    # Spot short simulation via inverse PnL (framework supports signal shorts)
                    equity = cash + position.quantity * (position.entry_price - mark)
            risk.update_equity(equity, cash=cash)
            equity_points.append((ts, equity))

            if position is not None:
                continue

            # Generate signal only from past+current closed bar
            signal = self.strategy.generate_signal(window)
            if signal is None or signal.signal_type == SignalType.NONE:
                continue

            signals.append(
                {
                    "timestamp": ts.isoformat(),
                    "type": signal.signal_type.value,
                    "entry": signal.entry_price,
                    "sl": signal.stop_loss,
                    "tp": signal.take_profit,
                }
            )

            # Spot framework: only long entries by default for BUY; SELL opens short sim
            decision = risk.evaluate_entry(signal, self.filters, available_balance=cash)
            if not decision.allowed or decision.size is None or not decision.size.accepted:
                continue

            qty = round_quantity(decision.size.quantity, self.filters)
            if qty <= 0:
                continue

            raw_entry = float(row["close"])
            if signal.signal_type == SignalType.BUY:
                fill = raw_entry * (1.0 + slip)
                side = Side.BUY
            else:
                fill = raw_entry * (1.0 - slip)
                side = Side.SELL
            fill = round_price(fill, self.filters)
            fees = qty * fill * fee_rate
            slippage_cost = abs(fill - raw_entry) * qty
            notional = qty * fill

            if side == Side.BUY and notional + fees > cash:
                continue

            stop = float(signal.stop_loss) if signal.stop_loss is not None else fill * 0.98
            tp = float(signal.take_profit) if signal.take_profit is not None else fill * 1.04

            if side == Side.BUY:
                cash -= notional + fees
            else:
                # Short: credit proceeds minus fees (simplified spot short model)
                cash += notional - fees

            position = OpenBacktestPosition(
                position_id=new_id("pos_"),
                side=side,
                quantity=qty,
                entry_price=fill,
                stop_loss=stop,
                take_profit=tp,
                entry_time=ts,
                entry_fees=fees,
                entry_slippage=slippage_cost,
            )
            risk.register_open_position(self.settings.symbol, notional)
            if decision.size:
                # client id tracking unused in backtest
                pass

        # Force close at end
        if position is not None:
            last = df.iloc[-1]
            ts = df.index[-1].to_pydatetime() if hasattr(df.index[-1], "to_pydatetime") else df.index[-1]
            trade, cash = self._close(
                position, float(last["close"]), ts, cash, fee_rate, slip, "eod"
            )
            trades.append(trade)
            risk.record_realized_trade(trade.net_pnl)
            equity_points.append((ts, cash))

        equity_curve = pd.Series(
            {t: e for t, e in equity_points},
            dtype=float,
        ).sort_index()
        equity_curve, dd_curve = equity_and_drawdown_frames(equity_curve)
        metrics = compute_metrics(
            trades,
            equity_curve,
            initial_capital=self.settings.initial_capital,
        )
        return BacktestResult(metrics, trades, equity_curve, dd_curve, signals)

    def _normalize(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        cols = {c.lower(): c for c in df.columns}
        rename = {}
        for need in ("open", "high", "low", "close", "volume"):
            if need in df.columns:
                continue
            if need in cols:
                rename[cols[need]] = need
        if rename:
            df = df.rename(columns=rename)
        for need in ("open", "high", "low", "close"):
            if need not in df.columns:
                raise ValueError(f"OHLCV missing column: {need}")
        if not isinstance(df.index, pd.DatetimeIndex):
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, unit=None)
                # handle ms integers
                if pd.api.types.is_numeric_dtype(data["timestamp"]):
                    unit = "ms" if data["timestamp"].iloc[0] > 10_000_000_000 else "s"
                    df["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, unit=unit)
                df = df.set_index("timestamp")
            else:
                raise ValueError("Data must have DatetimeIndex or timestamp column")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        return df.sort_index()

    def _check_exit(
        self, position: OpenBacktestPosition, row: pd.Series
    ) -> tuple[float | None, str | None]:
        high = float(row["high"])
        low = float(row["low"])
        # Conservative: if both SL and TP touched, assume SL first
        if position.side == Side.BUY:
            if low <= position.stop_loss:
                return position.stop_loss, "stop_loss"
            if high >= position.take_profit:
                return position.take_profit, "take_profit"
        else:
            if high >= position.stop_loss:
                return position.stop_loss, "stop_loss"
            if low <= position.take_profit:
                return position.take_profit, "take_profit"
        return None, None

    def _close(
        self,
        position: OpenBacktestPosition,
        exit_price: float,
        ts: datetime,
        cash: float,
        fee_rate: float,
        slip: float,
        reason: str,
    ) -> tuple[TradeRecord, float]:
        if position.side == Side.BUY:
            fill = exit_price * (1.0 - slip) if reason != "eod" else exit_price * (1.0 - slip)
            fees = position.quantity * fill * fee_rate
            slippage = abs(exit_price - fill) * position.quantity + position.entry_slippage
            proceeds = position.quantity * fill
            cash += proceeds - fees
            gross = (fill - position.entry_price) * position.quantity
        else:
            fill = exit_price * (1.0 + slip)
            fees = position.quantity * fill * fee_rate
            slippage = abs(exit_price - fill) * position.quantity + position.entry_slippage
            # Cover short
            cash -= position.quantity * fill + fees
            gross = (position.entry_price - fill) * position.quantity

        total_fees = position.entry_fees + fees
        net = gross - total_fees
        risk_unit = abs(position.entry_price - position.stop_loss)
        r_mult = (net / (risk_unit * position.quantity)) if risk_unit > 0 else None
        duration = max(0.0, (ts - position.entry_time).total_seconds())
        trade = TradeRecord(
            position_id=position.position_id,
            symbol=self.settings.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=fill,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            gross_pnl=gross,
            fees=total_fees,
            slippage=slippage,
            net_pnl=net,
            r_multiple=r_mult,
            opened_at=position.entry_time,
            closed_at=ts,
            duration_seconds=duration,
        )
        return trade, cash
