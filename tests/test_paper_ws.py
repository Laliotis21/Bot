"""Paper trading and websocket tests."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from app.config import Settings
from app.monitoring.websocket import MarketDataMonitor
from app.paper import PaperTradingEngine
from app.risk import RiskManager
from app.strategies import EmaRsiStrategy
from tests.fakes import FakeExchange


def test_paper_engine_runs_pipeline(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        paper_trading=True,
        initial_capital=500,
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        ema_fast=5,
        ema_slow=20,
        rsi_period=5,
    )
    engine = PaperTradingEngine(settings, EmaRsiStrategy.from_settings(settings), FakeExchange())
    idx = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    close = np.linspace(100, 120, 100)
    df = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1},
        index=idx,
    )
    result = engine.on_market_data(df)
    assert "action" in result
    engine.shutdown()


def test_stale_market_data_blocks_entries() -> None:
    settings = Settings(_env_file=None, paper_trading=True, stale_data_seconds=0.05)
    risk = RiskManager(settings)
    mon = MarketDataMonitor(risk, stale_after_seconds=0.05)
    mon.on_message({"price": 1})
    time.sleep(0.08)
    mon.poll_watchdog()
    assert risk.market_data_stale is True
