"""Exchange adapter abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.core.enums import OrderType, Side
from app.core.models import ExchangeFilters, Order


class ExchangeAdapter(ABC):
    """Exchange-agnostic trading interface.

    Binance Spot and Binance.US should use separate adapter configurations.
    """

    name: str = "base"

    @abstractmethod
    def fetch_balance(self) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: int | None = None,
        limit: int = 500,
    ) -> list[list[float]]: ...

    @abstractmethod
    def fetch_ticker(self, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def create_order(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    def cancel_all_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    def load_markets(self) -> dict[str, Any]: ...

    @abstractmethod
    def get_symbol_filters(self, symbol: str) -> ExchangeFilters: ...
