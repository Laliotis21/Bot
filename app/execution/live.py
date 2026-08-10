"""Live trading runner with explicit safety confirmation."""

from __future__ import annotations

from app import STARTUP_WARNING
from app.config.settings import LIVE_CONFIRMATION_PHRASE, Settings
from app.accounting.ledger import AccountingLedger
from app.exchanges.base import ExchangeAdapter
from app.execution.engine import ExecutionEngine
from app.execution.reconciliation import ReconciliationService
from app.monitoring.alerts import AlertService, ConsoleAlertService
from app.monitoring.websocket import MarketDataMonitor
from app.persistence.store import create_store
from app.portfolio.manager import PositionManager
from app.risk.manager import RiskManager
from app.strategies.base import Strategy
from app.utils.logging import get_logger, setup_logging

logger = get_logger("trading.live")


class LiveTradingGate:
    """Refuse to start live trading without explicit confirmation."""

    @staticmethod
    def assert_safe(settings: Settings) -> None:
        print(STARTUP_WARNING)
        if settings.paper_trading:
            raise RuntimeError("LiveTradingGate called while PAPER_TRADING=true")
        if settings.live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
            raise RuntimeError(
                "Refusing to start live trading: set "
                f"LIVE_TRADING_CONFIRMATION={LIVE_CONFIRMATION_PHRASE}"
            )


def build_live_stack(
    settings: Settings,
    strategy: Strategy,
    exchange: ExchangeAdapter,
    alert_service: AlertService | None = None,
) -> dict:
    """Assemble live components after safety checks."""
    LiveTradingGate.assert_safe(settings)
    setup_logging(settings.log_level, settings.logs_dir)
    store = create_store(settings.database_url)
    alerts = alert_service or ConsoleAlertService()
    risk = RiskManager(settings, store=store)
    ledger = AccountingLedger(exchange=settings.exchange)
    positions = PositionManager()
    execution = ExecutionEngine(
        exchange=exchange,
        settings=settings,
        risk=risk,
        ledger=ledger,
        store=store,
        paper=False,
    )
    reconciliation = ReconciliationService(exchange, positions, execution, alerts)
    report = reconciliation.reconcile(settings.symbol)
    if not report.ok:
        logger.critical("Startup reconciliation mismatches: %s", report.mismatches)
    monitor = MarketDataMonitor(risk, stale_after_seconds=settings.stale_data_seconds)
    alerts.send("Bot started (LIVE TRADING)")
    return {
        "store": store,
        "risk": risk,
        "ledger": ledger,
        "positions": positions,
        "execution": execution,
        "reconciliation": reconciliation,
        "monitor": monitor,
        "strategy": strategy,
        "alerts": alerts,
    }
