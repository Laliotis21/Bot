"""Order reconciliation after restarts — exchange is source of truth."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.models import Position
from app.exchanges.base import ExchangeAdapter
from app.execution.engine import ExecutionEngine
from app.monitoring.alerts import AlertService, ConsoleAlertService
from app.portfolio.manager import PositionManager
from app.utils.logging import get_logger

logger = get_logger("trading.reconciliation")


@dataclass
class ReconciliationReport:
    balance: dict[str, Any] = field(default_factory=dict)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    exchange_positions: list[dict[str, Any]] = field(default_factory=list)
    local_positions: list[Position] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


class ReconciliationService:
    """Compare exchange state vs local state and reconcile safely."""

    def __init__(
        self,
        exchange: ExchangeAdapter,
        positions: PositionManager,
        execution: ExecutionEngine | None = None,
        alert_service: AlertService | None = None,
    ) -> None:
        self.exchange = exchange
        self.positions = positions
        self.execution = execution
        self.alerts = alert_service or ConsoleAlertService()

    def reconcile(self, symbol: str | None = None) -> ReconciliationReport:
        report = ReconciliationReport()
        try:
            report.balance = self.exchange.fetch_balance()
        except Exception as exc:
            report.mismatches.append(f"balance_fetch_failed:{type(exc).__name__}")
            self.alerts.send(f"Reconciliation: balance fetch failed ({type(exc).__name__})")
            return report

        try:
            report.open_orders = self.exchange.fetch_open_orders(symbol)
        except Exception as exc:
            report.mismatches.append(f"open_orders_fetch_failed:{type(exc).__name__}")

        try:
            report.exchange_positions = self.exchange.fetch_positions(symbol)
        except Exception as exc:
            report.mismatches.append(f"positions_fetch_failed:{type(exc).__name__}")

        report.local_positions = list(self.positions.open_positions)

        # Spot: infer holdings from free balances vs local positions
        free = (report.balance.get("free") or {}) if isinstance(report.balance, dict) else {}
        for pos in report.local_positions:
            base = pos.symbol.split("/")[0]
            exchange_qty = float(free.get(base) or 0.0)
            if exchange_qty + 1e-8 < pos.quantity:
                msg = (
                    f"Local position {pos.position_id} qty={pos.quantity} "
                    f"exceeds exchange free {base}={exchange_qty}"
                )
                report.mismatches.append(msg)
                report.actions_taken.append("prefer_exchange_balance")
                logger.warning("Reconciliation mismatch: %s", msg)

        # Local open orders not on exchange
        if self.execution:
            exchange_ids = {str(o.get("id")) for o in report.open_orders}
            for order in self.execution.orders.values():
                if order.is_terminal:
                    continue
                if order.exchange_order_id and order.exchange_order_id not in exchange_ids:
                    report.mismatches.append(
                        f"Local open order {order.order_id} missing on exchange"
                    )
                    report.actions_taken.append(f"mark_stale_order:{order.order_id}")

        if report.mismatches:
            self.alerts.send(
                "Reconciliation mismatch detected:\n" + "\n".join(report.mismatches[:10])
            )
        else:
            logger.info("Reconciliation OK")
            report.actions_taken.append("none")
        return report
