"""Core domain models used across modules."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.core.enums import (
    OrderStatus,
    OrderType,
    PositionStatus,
    RiskEventType,
    Side,
    SignalType,
)


def utc_now() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    """Generate a unique identifier."""
    uid = uuid4().hex
    return f"{prefix}{uid}" if prefix else uid


class Signal(BaseModel):
    """Trading signal produced by a strategy.

    Strategies must NOT compute position size or place orders.
    """

    signal_type: SignalType
    symbol: str
    timestamp: datetime = Field(default_factory=utc_now)
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entry_price", "stop_loss", "take_profit")
    @classmethod
    def non_negative_prices(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("prices must be positive")
        return v


class ExchangeFilters(BaseModel):
    """Exchange precision / filter constraints for a symbol."""

    symbol: str
    min_qty: float = 0.0
    max_qty: float | None = None
    step_size: float = 0.0
    min_notional: float = 0.0
    tick_size: float = 0.0
    price_precision: int = 8
    quantity_precision: int = 8


class Order(BaseModel):
    """Normalized order representation."""

    order_id: str = Field(default_factory=lambda: new_id("ord_"))
    client_order_id: str = Field(default_factory=lambda: new_id("cli_"))
    exchange_order_id: str | None = None
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    average_price: float | None = None
    fee_amount: float = 0.0
    fee_currency: str = "USDT"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    position_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }


class Fill(BaseModel):
    """A single execution / partial fill."""

    fill_id: str = Field(default_factory=lambda: new_id("fill_"))
    order_id: str
    trade_id: str | None = None
    symbol: str
    side: Side
    quantity: float
    price: float
    fee_amount: float = 0.0
    fee_currency: str = "USDT"
    timestamp: datetime = Field(default_factory=utc_now)
    is_maker: bool = False


class Position(BaseModel):
    """Open or closed position state."""

    position_id: str = Field(default_factory=lambda: new_id("pos_"))
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    fees: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    opened_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None
    status: PositionStatus = PositionStatus.OPEN
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountSnapshot(BaseModel):
    """Point-in-time account equity snapshot."""

    timestamp: datetime = Field(default_factory=utc_now)
    equity: float
    cash: float
    peak_equity: float
    drawdown_pct: float = 0.0
    open_positions: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskEvent(BaseModel):
    """Persisted risk-control event."""

    event_id: str = Field(default_factory=lambda: new_id("risk_"))
    event_type: RiskEventType
    timestamp: datetime = Field(default_factory=utc_now)
    message: str
    equity: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TradeRecord(BaseModel):
    """Completed round-trip trade for analytics."""

    trade_id: str = Field(default_factory=lambda: new_id("trd_"))
    position_id: str
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    exit_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    gross_pnl: float
    fees: float
    slippage: float
    net_pnl: float
    r_multiple: float | None = None
    opened_at: datetime
    closed_at: datetime
    duration_seconds: float = 0.0


class LedgerEntry(BaseModel):
    """Normalized accounting ledger row."""

    timestamp_utc: datetime
    exchange: str
    order_id: str
    trade_id: str | None = None
    client_order_id: str | None = None
    symbol: str
    side: Side
    quantity: float
    executed_price: float
    quote_amount: float
    fee_amount: float
    fee_currency: str
    gross_pnl: float | None = None
    cost_basis: float | None = None
    net_realized_pnl: float | None = None
    account_balance: float | None = None
    position_id: str | None = None


class CircuitBreakerState(BaseModel):
    """Persisted hard drawdown circuit-breaker state."""

    tripped: bool = False
    peak_equity: float
    current_equity: float
    drawdown_pct: float = 0.0
    tripped_at: datetime | None = None
    requires_reset: bool = False


# Decimal helper for precision math (optional use by sizing/accounting)
Money = Decimal
