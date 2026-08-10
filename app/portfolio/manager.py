"""Position lifecycle management."""

from __future__ import annotations

from app.core.enums import PositionStatus, Side
from app.core.models import Fill, Position, new_id, utc_now
from app.utils.logging import get_logger

logger = get_logger("trading.portfolio")


class PositionManager:
    """Track open/closed positions including partial fills and exits."""

    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}
        self.closed: list[Position] = []

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status != PositionStatus.CLOSED]

    def open_position(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: float,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        fees: float = 0.0,
        position_id: str | None = None,
    ) -> Position:
        pos = Position(
            position_id=position_id or new_id("pos_"),
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            fees=fees,
            status=PositionStatus.OPEN,
        )
        self.positions[pos.position_id] = pos
        logger.info(
            "Opened position %s %s qty=%.8f @ %.8f",
            pos.position_id,
            symbol,
            quantity,
            entry_price,
        )
        return pos

    def apply_fill(self, position_id: str, fill: Fill) -> Position:
        pos = self.positions[position_id]
        if fill.side == pos.side:
            # Scale-in / partial entry
            total_qty = pos.quantity + fill.quantity
            pos.entry_price = (
                (pos.entry_price * pos.quantity + fill.price * fill.quantity) / total_qty
                if total_qty
                else fill.price
            )
            pos.quantity = total_qty
        else:
            # Partial or full exit
            exit_qty = min(pos.quantity, fill.quantity)
            if pos.side == Side.BUY:
                pnl = (fill.price - pos.entry_price) * exit_qty
            else:
                pnl = (pos.entry_price - fill.price) * exit_qty
            pos.realized_pnl += pnl - fill.fee_amount
            pos.fees += fill.fee_amount
            pos.quantity -= exit_qty
            if pos.quantity <= 1e-12:
                pos.quantity = 0.0
                pos.status = PositionStatus.CLOSED
                pos.closed_at = utc_now()
                self.closed.append(pos)
                logger.info("Closed position %s net_realized=%.6f", pos.position_id, pos.realized_pnl)
            else:
                pos.status = PositionStatus.PARTIALLY_CLOSED
        pos.fees += fill.fee_amount if fill.side == pos.side else 0.0
        return pos

    def mark_unrealized(self, position_id: str, mark_price: float) -> float:
        pos = self.positions[position_id]
        if pos.status == PositionStatus.CLOSED:
            pos.unrealized_pnl = 0.0
            return 0.0
        if pos.side == Side.BUY:
            pos.unrealized_pnl = (mark_price - pos.entry_price) * pos.quantity
        else:
            pos.unrealized_pnl = (pos.entry_price - mark_price) * pos.quantity
        return pos.unrealized_pnl

    def get_by_symbol(self, symbol: str) -> list[Position]:
        return [p for p in self.open_positions if p.symbol == symbol]

    def orphaned_check(self, exchange_symbols: set[str]) -> list[Position]:
        """Return local opens not present on exchange symbol set."""
        return [p for p in self.open_positions if p.symbol not in exchange_symbols]
