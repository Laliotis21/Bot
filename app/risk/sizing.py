"""Position sizing helpers."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models import ExchangeFilters
from app.utils.precision import clamp_quantity


@dataclass(frozen=True)
class PositionSizeResult:
    """Result of dynamic position sizing."""

    quantity: float
    risk_amount: float
    risk_per_unit: float
    notional: float
    rejected_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.quantity > 0 and self.rejected_reason is None


def compute_position_size(
    *,
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    filters: ExchangeFilters,
    available_balance: float,
    max_exposure_pct: float = 1.0,
    max_position_pct: float = 1.0,
    current_exposure: float = 0.0,
    fee_rate: float = 0.0,
    slippage_rate: float = 0.0,
) -> PositionSizeResult:
    """Compute position size from equity risk and exchange constraints.

    Formula:
        risk_amount = equity * risk_pct
        position_size = risk_amount / abs(entry - stop)
    Then clamp to exchange filters, balance, exposure, and costs.
    """
    if equity <= 0:
        return PositionSizeResult(0.0, 0.0, 0.0, 0.0, "non_positive_equity")
    if entry_price <= 0 or stop_loss <= 0:
        return PositionSizeResult(0.0, 0.0, 0.0, 0.0, "invalid_prices")

    risk_per_unit = abs(entry_price - stop_loss)
    if risk_per_unit <= 0:
        return PositionSizeResult(0.0, 0.0, 0.0, 0.0, "zero_risk_per_unit")

    risk_amount = equity * risk_pct
    raw_qty = risk_amount / risk_per_unit

    # Reserve room for fees + slippage on entry
    cost_buffer = 1.0 + fee_rate + slippage_rate
    max_notional_by_balance = max(0.0, available_balance) / cost_buffer
    max_notional_by_exposure = max(0.0, equity * max_exposure_pct - current_exposure)
    max_notional_by_position = equity * max_position_pct
    max_notional = min(max_notional_by_balance, max_notional_by_exposure, max_notional_by_position)

    if max_notional <= 0:
        return PositionSizeResult(0.0, risk_amount, risk_per_unit, 0.0, "insufficient_capacity")

    qty_cap = max_notional / entry_price
    qty = min(raw_qty, qty_cap)
    qty = clamp_quantity(qty, entry_price, filters, available_quote=max_notional)

    if qty <= 0:
        return PositionSizeResult(0.0, risk_amount, risk_per_unit, 0.0, "filter_or_notional")

    notional = qty * entry_price
    return PositionSizeResult(qty, risk_amount, risk_per_unit, notional, None)
