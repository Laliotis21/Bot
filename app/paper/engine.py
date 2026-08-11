"""Paper trading engine — same pipeline as live, simulated fills."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.accounting.ledger import AccountingLedger
from app.config.settings import Settings
from app.core.enums import OrderType, Side, SignalType
from app.core.models import ExchangeFilters, Fill, Order
from app.exchanges.base import ExchangeAdapter
from app.execution.engine import ExecutionEngine
from app.monitoring.alerts import AlertService, ConsoleAlertService
from app.persistence.store import PersistenceStore, create_store
from app.portfolio.manager import PositionManager
from app.risk.manager import RiskManager
from app.strategies.base import Strategy
from app.utils.logging import get_logger

logger = get_logger("trading.paper")


class PaperTradingEngine:
    """Run strategy/risk/execution with simulated fills and full audit trail."""

    def __init__(
        self,
        settings: Settings,
        strategy: Strategy,
        exchange: ExchangeAdapter,
        *,
        store: PersistenceStore | None = None,
        alert_service: AlertService | None = None,
        filters: ExchangeFilters | None = None,
    ) -> None:
        if not settings.paper_trading:
            raise ValueError("PaperTradingEngine requires PAPER_TRADING=true")
        self.settings = settings
        self.strategy = strategy
        self.exchange = exchange
        self.store = store or create_store(settings.database_url)
        self.alerts = alert_service or ConsoleAlertService()
        self.risk = RiskManager(settings, store=self.store)
        self.ledger = AccountingLedger(
            exchange=settings.exchange,
            method=settings.cost_basis_method,
        )
        self.ledger.cash_balance = settings.initial_capital
        self.positions = PositionManager()
        self.filters = filters or ExchangeFilters(
            symbol=settings.symbol,
            min_qty=0.0001,
            step_size=0.0001,
            min_notional=10.0,
            tick_size=0.01,
        )
        self.execution = ExecutionEngine(
            exchange=exchange,
            settings=settings,
            risk=self.risk,
            ledger=self.ledger,
            store=self.store,
            paper=True,
            fill_simulator=self._simulate_fill,
        )
        self._last_price: float | None = None
        self.alerts.send("Bot started (PAPER TRADING)")

    def _simulate_fill(self, order: Order) -> Fill:
        px = order.price or self._last_price or 0.0
        slip = self.settings.slippage_rate
        if order.side == Side.BUY:
            px *= 1.0 + slip
        else:
            px *= 1.0 - slip
        fee = px * order.quantity * self.settings.effective_taker_fee
        return Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=px,
            fee_amount=fee,
            fee_currency=self.settings.currency,
        )

    def on_market_data(self, market_data: pd.DataFrame) -> dict[str, Any]:
        """Process one closed-bar update through the full pipeline.

        Spot paper mode is long-only: BUY opens, SELL closes an existing long.
        Short opens are skipped so the FIFO ledger never sells without lots.
        """
        if market_data.empty:
            return {"action": "noop"}
        self._last_price = float(market_data["close"].iloc[-1])
        signal = self.strategy.generate_signal(market_data)
        if signal is None or signal.signal_type == SignalType.NONE:
            return {"action": "no_signal"}

        if self.store:
            self.store.insert_json("strategy_signals", signal.model_dump(mode="json"))

        open_for_symbol = self.positions.get_by_symbol(signal.symbol)

        # Close existing long on SELL signal
        if signal.signal_type == SignalType.SELL:
            if not open_for_symbol:
                return {"action": "skipped_short", "reason": "spot_long_only"}
            pos = open_for_symbol[0]
            order = self.execution.submit(
                symbol=signal.symbol,
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=pos.quantity,
                price=signal.entry_price or self._last_price,
                position_id=pos.position_id,
            )
            fill = Fill(
                order_id=order.order_id,
                symbol=order.symbol,
                side=Side.SELL,
                quantity=min(pos.quantity, order.filled_quantity),
                price=order.average_price or 0.0,
                fee_amount=order.fee_amount,
            )
            # Ledger already recorded the SELL in ExecutionEngine.apply_fill
            self.positions.apply_fill(pos.position_id, fill)
            notional = pos.entry_price * fill.quantity
            self.risk.unregister_position(signal.symbol, notional)
            self.risk.record_realized_trade(pos.realized_pnl)
            self.alerts.send(f"Trade closed (paper): {pos.position_id}")
            return {"action": "closed", "position_id": pos.position_id, "order_id": order.order_id}

        # BUY — open long only when flat
        if open_for_symbol:
            return {"action": "already_open"}

        decision = self.risk.evaluate_entry(
            signal,
            self.filters,
            available_balance=self.ledger.cash_balance,
        )
        if not decision.allowed or not decision.size or not decision.size.accepted:
            return {"action": "risk_blocked", "reason": decision.reason}

        order = self.execution.submit(
            symbol=signal.symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=decision.size.quantity,
            price=signal.entry_price,
        )
        pos = self.positions.open_position(
            symbol=signal.symbol,
            side=Side.BUY,
            quantity=order.filled_quantity,
            entry_price=order.average_price or signal.entry_price or 0.0,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            fees=order.fee_amount,
        )
        self.risk.register_open_position(signal.symbol, decision.size.notional)
        self.alerts.send(f"Trade opened (paper): {pos.position_id} {signal.symbol}")
        return {"action": "opened", "position_id": pos.position_id, "order_id": order.order_id}

    def shutdown(self) -> None:
        self.alerts.send("Bot stopped (PAPER TRADING)")
