"""Binance Spot adapter via CCXT."""

from __future__ import annotations

import os
from typing import Any

import ccxt

from app.config.settings import Settings
from app.core.enums import OrderType, Side
from app.core.models import ExchangeFilters
from app.exchanges.base import ExchangeAdapter
from app.utils.logging import get_logger

logger = get_logger("trading.exchange")


class BinanceAdapter(ExchangeAdapter):
    """CCXT Binance Spot adapter.

    Credentials are read from settings/env and never logged.
    """

    name = "binance"

    def __init__(self, settings: Settings, *, sandbox: bool = False) -> None:
        self.settings = settings
        exchange_id = settings.exchange
        if exchange_id == "binanceus":
            klass = ccxt.binanceus
            self.name = "binanceus"
        else:
            klass = ccxt.binance
            self.name = "binance"

        config: dict[str, Any] = {
            "apiKey": settings.binance_api_key or os.getenv("BINANCE_API_KEY", ""),
            "secret": settings.binance_secret_key or os.getenv("BINANCE_SECRET_KEY", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        self.client = klass(config)
        if sandbox and hasattr(self.client, "set_sandbox_mode"):
            self.client.set_sandbox_mode(True)
        logger.info("Initialized %s spot adapter (sandbox=%s)", self.name, sandbox)

    def fetch_balance(self) -> dict[str, Any]:
        return self.client.fetch_balance()

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: int | None = None,
        limit: int = 500,
    ) -> list[list[float]]:
        return self.client.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self.client.fetch_ticker(symbol)

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self.client.fetch_open_orders(symbol)

    def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        # Spot has no true positions; return empty for interface compatibility
        return []

    def create_order(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        ccxt_type = {
            OrderType.MARKET: "market",
            OrderType.LIMIT: "limit",
            OrderType.STOP_LOSS: "market",
            OrderType.TAKE_PROFIT: "limit",
        }[order_type]
        if order_type == OrderType.STOP_LOSS and "stopPrice" not in params and price is not None:
            params["stopPrice"] = price
        return self.client.create_order(
            symbol,
            ccxt_type,
            side.value.lower(),
            amount,
            price,
            params,
        )

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return self.client.cancel_order(order_id, symbol)

    def cancel_all_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if hasattr(self.client, "cancel_all_orders") and symbol:
            return self.client.cancel_all_orders(symbol)
        open_orders = self.fetch_open_orders(symbol)
        results = []
        for o in open_orders:
            results.append(self.cancel_order(o["id"], o.get("symbol") or symbol or ""))
        return results

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return self.client.fetch_order(order_id, symbol)

    def load_markets(self) -> dict[str, Any]:
        return self.client.load_markets()

    def get_symbol_filters(self, symbol: str) -> ExchangeFilters:
        markets = self.load_markets()
        m = markets.get(symbol) or markets.get(symbol.replace("/", ""))
        if not m:
            return ExchangeFilters(symbol=symbol)
        precision = m.get("precision") or {}
        limits = m.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        price_limits = limits.get("price") or {}
        info = m.get("info") or {}

        step = float(amount_limits.get("min") or 0) and float(
            m.get("precision", {}).get("amount") or 0
        )
        # Prefer filter info when present
        tick = float(price_limits.get("min") or 0) or 0.0
        filters = info.get("filters") if isinstance(info, dict) else None
        min_qty = float(amount_limits.get("min") or 0.0)
        min_notional = float(cost_limits.get("min") or 0.0)
        step_size = 0.0
        tick_size = tick
        if isinstance(filters, list):
            for f in filters:
                ftype = f.get("filterType")
                if ftype == "LOT_SIZE":
                    min_qty = float(f.get("minQty") or min_qty)
                    step_size = float(f.get("stepSize") or 0.0)
                elif ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
                    min_notional = float(f.get("minNotional") or f.get("notional") or min_notional)
                elif ftype == "PRICE_FILTER":
                    tick_size = float(f.get("tickSize") or tick_size)

        price_prec = int(precision.get("price") or 8) if not isinstance(precision.get("price"), float) else 8
        qty_prec = int(precision.get("amount") or 8) if not isinstance(precision.get("amount"), float) else 8
        if isinstance(precision.get("price"), float):
            price_prec = max(0, abs(str(precision["price"])[::-1].find(".")))
        if isinstance(precision.get("amount"), float):
            qty_prec = max(0, abs(str(precision["amount"])[::-1].find(".")))

        return ExchangeFilters(
            symbol=symbol,
            min_qty=min_qty,
            step_size=step_size or (10 ** -qty_prec if qty_prec else 0.0),
            min_notional=min_notional,
            tick_size=tick_size or (10 ** -price_prec if price_prec else 0.0),
            price_precision=price_prec if isinstance(price_prec, int) else 8,
            quantity_precision=qty_prec if isinstance(qty_prec, int) else 8,
        )


def create_exchange(settings: Settings, *, sandbox: bool = False) -> ExchangeAdapter:
    """Factory for configured exchange adapters."""
    if settings.exchange in {"binance", "binanceus"}:
        return BinanceAdapter(settings, sandbox=sandbox)
    raise ValueError(f"Unsupported exchange: {settings.exchange}")
