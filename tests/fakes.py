"""Shared fakes for exchange tests — never hits a real exchange."""

from __future__ import annotations

from typing import Any

from app.core.enums import OrderType, Side
from app.core.models import ExchangeFilters
from app.exchanges.base import ExchangeAdapter


class FakeExchange(ExchangeAdapter):
    name = "fake"

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.balances = {"free": {"USDT": 500.0, "BTC": 0.0}, "total": {"USDT": 500.0}}
        self._next_id = 1
        self.fail_next = False
        self.reject_next = False
        self.partial_fill = False

    def fetch_balance(self) -> dict[str, Any]:
        return self.balances

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=500):
        return [[1_700_000_000_000, 100, 101, 99, 100, 1]] * min(limit, 5)

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "last": 100.0}

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [o for o in self.orders.values() if o.get("status") == "open"]

    def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return []

    def create_order(self, symbol, side, order_type, amount, price=None, params=None):
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("simulated timeout")
        if self.reject_next:
            self.reject_next = False
            raise Exception("insufficient balance")
        oid = str(self._next_id)
        self._next_id += 1
        filled = amount * 0.5 if self.partial_fill else amount
        if self.partial_fill:
            self.partial_fill = False
        order = {
            "id": oid,
            "clientOrderId": (params or {}).get("newClientOrderId"),
            "symbol": symbol,
            "side": side.value.lower() if isinstance(side, Side) else side,
            "type": order_type.value.lower() if isinstance(order_type, OrderType) else order_type,
            "amount": amount,
            "filled": filled,
            "average": price or 100.0,
            "status": "open" if filled < amount else "closed",
            "fees": [{"cost": filled * (price or 100) * 0.00075}],
        }
        self.orders[oid] = order
        return order

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        o = self.orders.get(order_id, {"id": order_id})
        o["status"] = "canceled"
        self.orders[order_id] = o
        return o

    def cancel_all_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        out = []
        for oid in list(self.orders):
            out.append(self.cancel_order(oid, symbol or ""))
        return out

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return self.orders[order_id]

    def load_markets(self) -> dict[str, Any]:
        return {
            "BTC/USDT": {
                "precision": {"price": 2, "amount": 4},
                "limits": {
                    "amount": {"min": 0.0001},
                    "cost": {"min": 10},
                    "price": {"min": 0.01},
                },
                "info": {
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    ]
                },
            }
        }

    def get_symbol_filters(self, symbol: str) -> ExchangeFilters:
        return ExchangeFilters(
            symbol=symbol,
            min_qty=0.0001,
            step_size=0.0001,
            min_notional=10.0,
            tick_size=0.01,
            price_precision=2,
            quantity_precision=4,
        )
