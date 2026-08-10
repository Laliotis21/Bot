"""Dedicated risk management engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from app.config.settings import Settings
from app.core.enums import RiskEventType, Side
from app.core.models import CircuitBreakerState, ExchangeFilters, RiskEvent, Signal, utc_now
from app.monitoring.alerts import AlertService, ConsoleAlertService
from app.persistence.store import PersistenceStore
from app.risk.sizing import PositionSizeResult, compute_position_size
from app.utils.logging import get_logger

logger = get_logger("trading.risk")


@dataclass
class RiskDecision:
    """Outcome of a pre-trade risk check."""

    allowed: bool
    reason: str | None = None
    size: PositionSizeResult | None = None
    events: list[RiskEvent] = field(default_factory=list)


class RiskManager:
    """Capital-preservation risk controls.

    Responsibilities:
    - dynamic position sizing from current equity
    - hard drawdown circuit breaker (persisted across restarts)
    - UTC daily loss guard
    - consecutive losses, trade-count, exposure, duplicate guards
    - kill switch, API-error circuit, stale market-data halt
    """

    def __init__(
        self,
        settings: Settings,
        store: PersistenceStore | None = None,
        alert_service: AlertService | None = None,
        *,
        equity: float | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.alerts = alert_service or ConsoleAlertService()

        starting = equity if equity is not None else settings.initial_capital
        self.equity = starting
        self.peak_equity = starting
        self.cash = starting

        self.kill_switch = False
        self.circuit_tripped = False
        self.circuit_requires_reset = False

        self.daily_start_equity = starting
        self.daily_date: date = utc_now().date()
        self.daily_entries_blocked_until: datetime | None = None
        self.trades_today = 0
        self.consecutive_losses = 0

        self.open_symbols: set[str] = set()
        self.pending_client_order_ids: set[str] = set()
        self.current_exposure = 0.0
        self.api_error_count = 0
        self.market_data_stale = False
        self._events: list[RiskEvent] = []

        self._restore_circuit_breaker()

    # ------------------------------------------------------------------
    # Equity / lifecycle
    # ------------------------------------------------------------------
    def update_equity(self, equity: float, *, cash: float | None = None) -> None:
        """Update equity after marks or realized PnL; refresh peak & daily window."""
        self.equity = equity
        if cash is not None:
            self.cash = cash
        if equity > self.peak_equity:
            self.peak_equity = equity
        self._roll_daily_if_needed()
        self._persist_circuit_state()
        self._check_drawdown_circuit()
        self._check_daily_loss()

    def record_realized_trade(self, net_pnl: float, *, notional_closed: float = 0.0) -> None:
        """Recalculate equity after a realized trade and update loss streaks."""
        self.equity += net_pnl
        self.cash += net_pnl
        if notional_closed:
            self.current_exposure = max(0.0, self.current_exposure - abs(notional_closed))
        if net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self.trades_today += 1
        self.update_equity(self.equity, cash=self.cash)

    def register_open_position(self, symbol: str, notional: float) -> None:
        self.open_symbols.add(symbol)
        self.current_exposure += abs(notional)

    def unregister_position(self, symbol: str, notional: float = 0.0) -> None:
        self.open_symbols.discard(symbol)
        if notional:
            self.current_exposure = max(0.0, self.current_exposure - abs(notional))

    def register_client_order_id(self, client_order_id: str) -> None:
        self.pending_client_order_ids.add(client_order_id)

    def clear_client_order_id(self, client_order_id: str) -> None:
        self.pending_client_order_ids.discard(client_order_id)

    def set_kill_switch(self, enabled: bool = True, reason: str = "manual") -> None:
        self.kill_switch = enabled
        if enabled:
            self._emit(RiskEventType.KILL_SWITCH, f"Kill switch engaged: {reason}")
            self.alerts.send(f"EMERGENCY: kill switch engaged ({reason})")

    def record_api_error(self) -> None:
        self.api_error_count += 1
        if self.api_error_count >= self.settings.api_error_threshold:
            self._emit(
                RiskEventType.API_ERROR_CIRCUIT,
                f"API error circuit: {self.api_error_count} consecutive errors",
            )
            self.alerts.send("API failure circuit breaker triggered")

    def clear_api_errors(self) -> None:
        self.api_error_count = 0

    def set_market_data_stale(self, stale: bool) -> None:
        self.market_data_stale = stale
        if stale:
            self._emit(RiskEventType.STALE_MARKET_DATA, "Market data stale — blocking new entries")
            self.alerts.send("Stale market data — new entries blocked")

    def reset_circuit_breaker(self, *, explicit: bool = True) -> None:
        """Explicit reset required before trading resumes after hard drawdown."""
        if not explicit:
            raise ValueError("Circuit breaker reset must be explicit")
        self.circuit_tripped = False
        self.circuit_requires_reset = False
        self.peak_equity = max(self.peak_equity, self.equity)
        self._persist_circuit_state()
        logger.warning("Circuit breaker reset explicitly by operator")

    # ------------------------------------------------------------------
    # Sizing & pre-trade gate
    # ------------------------------------------------------------------
    def size_position(
        self,
        *,
        entry_price: float,
        stop_loss: float,
        filters: ExchangeFilters,
        available_balance: float | None = None,
    ) -> PositionSizeResult:
        """Dynamic position size from current equity and risk %."""
        bal = self.cash if available_balance is None else available_balance
        return compute_position_size(
            equity=self.equity,
            risk_pct=self.settings.risk_per_trade,
            entry_price=entry_price,
            stop_loss=stop_loss,
            filters=filters,
            available_balance=bal,
            max_exposure_pct=self.settings.max_portfolio_exposure,
            max_position_pct=self.settings.max_position_size,
            current_exposure=self.current_exposure,
            fee_rate=self.settings.effective_taker_fee,
            slippage_rate=self.settings.slippage_rate,
        )

    def evaluate_entry(
        self,
        signal: Signal,
        filters: ExchangeFilters,
        *,
        available_balance: float | None = None,
        client_order_id: str | None = None,
        open_position_count: int | None = None,
    ) -> RiskDecision:
        """Full pre-trade risk gate. Never places orders."""
        self._roll_daily_if_needed()
        events: list[RiskEvent] = []

        if signal.signal_type.value == "NONE":
            return RiskDecision(False, "no_signal", events=events)

        if self.kill_switch:
            ev = self._emit(RiskEventType.KILL_SWITCH, "Entry blocked by kill switch", persist=False)
            return RiskDecision(False, "kill_switch", events=[ev])

        if self.circuit_tripped or self.circuit_requires_reset:
            return RiskDecision(False, "circuit_breaker", events=events)

        if self.market_data_stale:
            return RiskDecision(False, "stale_market_data", events=events)

        if self.api_error_count >= self.settings.api_error_threshold:
            return RiskDecision(False, "api_error_circuit", events=events)

        if self.daily_entries_blocked_until and utc_now() < self.daily_entries_blocked_until:
            return RiskDecision(False, "daily_loss_guard", events=events)

        if self.trades_today >= self.settings.max_trades_per_day:
            ev = self._emit(RiskEventType.MAX_TRADES_PER_DAY, "Max trades per day reached")
            return RiskDecision(False, "max_trades_per_day", events=[ev])

        if self.consecutive_losses >= self.settings.max_consecutive_losses:
            ev = self._emit(
                RiskEventType.MAX_CONSECUTIVE_LOSSES,
                f"Max consecutive losses ({self.consecutive_losses})",
            )
            return RiskDecision(False, "max_consecutive_losses", events=[ev])

        open_count = open_position_count if open_position_count is not None else len(self.open_symbols)
        if open_count >= self.settings.max_open_positions:
            return RiskDecision(False, "max_open_positions", events=events)

        if signal.symbol in self.open_symbols:
            ev = self._emit(
                RiskEventType.DUPLICATE_POSITION,
                f"Duplicate position blocked for {signal.symbol}",
            )
            return RiskDecision(False, "duplicate_position", events=[ev])

        if client_order_id and client_order_id in self.pending_client_order_ids:
            ev = self._emit(
                RiskEventType.DUPLICATE_ORDER,
                f"Duplicate order id blocked: {client_order_id}",
            )
            return RiskDecision(False, "duplicate_order", events=[ev])

        if signal.entry_price is None or signal.stop_loss is None:
            return RiskDecision(False, "missing_prices", events=events)

        size = self.size_position(
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            filters=filters,
            available_balance=available_balance,
        )
        if not size.accepted:
            ev = self._emit(
                RiskEventType.FILTER_VIOLATION if size.rejected_reason == "filter_or_notional"
                else RiskEventType.INSUFFICIENT_BALANCE,
                f"Sizing rejected: {size.rejected_reason}",
                persist=False,
            )
            return RiskDecision(False, size.rejected_reason, size=size, events=[ev])

        exposure_pct = (self.current_exposure + size.notional) / self.equity if self.equity else 1.0
        if exposure_pct > self.settings.max_portfolio_exposure + 1e-12:
            ev = self._emit(RiskEventType.MAX_EXPOSURE, "Max portfolio exposure exceeded")
            return RiskDecision(False, "max_exposure", size=size, events=[ev])

        return RiskDecision(True, None, size=size, events=events)

    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    def daily_drawdown_pct(self) -> float:
        """Negative when losing vs daily start."""
        if self.daily_start_equity <= 0:
            return 0.0
        return (self.equity - self.daily_start_equity) / self.daily_start_equity

    def on_circuit_breaker_actions(self) -> dict[str, bool]:
        """Actions required when hard drawdown trips."""
        return {
            "stop_new_positions": True,
            "cancel_entry_orders": True,
            "manage_or_close_existing": self.settings.emergency_close_on_drawdown,
            "persist_state": True,
            "send_alert": True,
            "require_explicit_reset": True,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _roll_daily_if_needed(self) -> None:
        today = utc_now().date()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_start_equity = self.equity
            self.trades_today = 0
            logger.info("UTC trading day rolled; daily starting equity=%.4f", self.equity)

    def _check_drawdown_circuit(self) -> None:
        dd = self.drawdown_pct()
        if dd >= self.settings.max_drawdown and not self.circuit_tripped:
            self.circuit_tripped = True
            self.circuit_requires_reset = True
            self._emit(
                RiskEventType.DRAWDOWN_CIRCUIT_BREAKER,
                f"Hard drawdown circuit breaker tripped at {dd:.2%}",
            )
            self.alerts.send(
                f"EMERGENCY: drawdown circuit breaker tripped at {dd:.2%}. "
                "Trading halted until explicit reset."
            )
            self._persist_circuit_state()

    def _check_daily_loss(self) -> None:
        daily_dd = self.daily_drawdown_pct()
        if daily_dd <= -self.settings.max_daily_loss:
            until = utc_now() + timedelta(hours=24)
            self.daily_entries_blocked_until = until
            self._emit(
                RiskEventType.DAILY_LOSS_GUARD,
                f"Daily loss guard triggered ({daily_dd:.2%}); entries blocked until {until.isoformat()}",
            )
            self.alerts.send(f"Daily loss limit triggered ({daily_dd:.2%})")

    def _emit(
        self,
        event_type: RiskEventType,
        message: str,
        *,
        persist: bool = True,
    ) -> RiskEvent:
        event = RiskEvent(
            event_type=event_type,
            message=message,
            equity=self.equity,
            metadata={"peak_equity": self.peak_equity, "drawdown_pct": self.drawdown_pct()},
        )
        self._events.append(event)
        logger.warning("Risk event %s: %s", event_type.value, message)
        if persist and self.store is not None:
            self.store.insert_json("risk_events", event.model_dump(mode="json"))
        return event

    def _persist_circuit_state(self) -> None:
        if self.store is None:
            return
        state = CircuitBreakerState(
            tripped=self.circuit_tripped,
            peak_equity=self.peak_equity,
            current_equity=self.equity,
            drawdown_pct=self.drawdown_pct(),
            tripped_at=utc_now() if self.circuit_tripped else None,
            requires_reset=self.circuit_requires_reset,
        )
        self.store.save_circuit_breaker(state)

    def _restore_circuit_breaker(self) -> None:
        if self.store is None:
            return
        state = self.store.load_circuit_breaker()
        if state is None:
            self._persist_circuit_state()
            return
        self.peak_equity = max(state.peak_equity, self.equity)
        self.circuit_tripped = state.tripped
        self.circuit_requires_reset = state.requires_reset
        if state.tripped:
            logger.critical(
                "Restored tripped circuit breaker from persistence (peak=%.4f equity=%.4f)",
                self.peak_equity,
                self.equity,
            )


def example_sizing_from_spec() -> PositionSizeResult:
    """Documented example: $500, 1.5%, entry 100, stop 95 -> 1.5 units."""
    filters = ExchangeFilters(symbol="EX", min_qty=0.0, step_size=0.0, min_notional=0.0)
    return compute_position_size(
        equity=500.0,
        risk_pct=0.015,
        entry_price=100.0,
        stop_loss=95.0,
        filters=filters,
        available_balance=500.0,
        fee_rate=0.0,
        slippage_rate=0.0,
    )
