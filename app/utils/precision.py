"""Precision and exchange-filter helpers."""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal

from app.core.models import ExchangeFilters


def round_to_step(value: float, step: float, *, rounding=ROUND_DOWN) -> float:
    """Round ``value`` down (by default) to the nearest ``step`` increment."""
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    quantized = (d_value / d_step).to_integral_value(rounding=rounding) * d_step
    return float(quantized)


def round_price(price: float, filters: ExchangeFilters) -> float:
    """Round price to tick size / precision."""
    if filters.tick_size > 0:
        return round_to_step(price, filters.tick_size)
    return float(f"{price:.{filters.price_precision}f}")


def round_quantity(qty: float, filters: ExchangeFilters) -> float:
    """Round quantity to lot step / precision."""
    if filters.step_size > 0:
        return round_to_step(qty, filters.step_size)
    return float(f"{qty:.{filters.quantity_precision}f}")


def meets_min_notional(qty: float, price: float, filters: ExchangeFilters) -> bool:
    """Return True if qty * price satisfies min notional."""
    return (qty * price) >= filters.min_notional


def clamp_quantity(
    qty: float,
    price: float,
    filters: ExchangeFilters,
    *,
    available_quote: float | None = None,
) -> float:
    """Clamp quantity to exchange filters and optional available balance."""
    q = round_quantity(qty, filters)
    if filters.min_qty and q < filters.min_qty:
        return 0.0
    if filters.max_qty is not None:
        q = min(q, filters.max_qty)
        q = round_quantity(q, filters)
    if available_quote is not None and price > 0:
        max_affordable = available_quote / price
        q = min(q, round_quantity(max_affordable, filters))
    if not meets_min_notional(q, price, filters):
        return 0.0
    if q <= 0 or math.isnan(q) or math.isinf(q):
        return 0.0
    return q
