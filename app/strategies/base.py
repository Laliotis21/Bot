"""Abstract strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from app.core.models import Signal


class Strategy(ABC):
    """Strategy produces signals only — no sizing, no orders."""

    name: str = "base"

    @abstractmethod
    def generate_signal(self, market_data: pd.DataFrame) -> Signal | None:
        """Generate a trading signal from historical OHLCV data.

        Implementations must only use information available in ``market_data``
        and must not peek at future candles.
        """
