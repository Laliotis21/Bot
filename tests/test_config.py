"""Phase 1 tests: configuration and core models."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app import STARTUP_WARNING, __version__
from app.config import LIVE_CONFIRMATION_PHRASE, Settings
from app.core import Signal, SignalType
from app.utils.logging import setup_logging
from app.utils.precision import clamp_quantity, round_quantity
from app.core.models import ExchangeFilters


def test_version_and_warning() -> None:
    assert __version__
    assert "real financial orders" in STARTUP_WARNING


def test_default_settings_are_paper() -> None:
    s = Settings(
        _env_file=None,
        paper_trading=True,
        binance_api_key="",
        binance_secret_key="",
    )
    assert s.paper_trading is True
    assert s.initial_capital == 500.0
    assert s.risk_per_trade == 0.015
    assert s.target_rr == 2.0
    assert s.max_drawdown == 0.15
    assert s.max_daily_loss == 0.04
    assert s.maker_fee == 0.00075
    assert s.exchange == "binance"


def test_live_requires_confirmation() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            paper_trading=False,
            live_trading_confirmation="",
            binance_api_key="k",
            binance_secret_key="s",
        )


def test_live_with_confirmation() -> None:
    s = Settings(
        _env_file=None,
        paper_trading=False,
        live_trading_confirmation=LIVE_CONFIRMATION_PHRASE,
        binance_api_key="k",
        binance_secret_key="s",
    )
    assert s.paper_trading is False


def test_invalid_risk_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, risk_per_trade=0)


def test_signal_model() -> None:
    sig = Signal(signal_type=SignalType.BUY, symbol="BTC/USDT", entry_price=100.0, stop_loss=95.0)
    assert sig.signal_type == SignalType.BUY
    assert sig.take_profit is None


def test_logging_setup(tmp_path) -> None:
    logger = setup_logging(level="DEBUG", logs_dir=tmp_path, name="test_logger")
    logger.info("startup ok")
    assert (tmp_path / "trading.log").exists()


def test_precision_clamp() -> None:
    filters = ExchangeFilters(
        symbol="BTC/USDT",
        min_qty=0.001,
        step_size=0.001,
        min_notional=10.0,
        tick_size=0.01,
    )
    q = clamp_quantity(1.23456, 100.0, filters)
    assert q == round_quantity(1.234, filters) or q == 1.234
    assert clamp_quantity(0.0001, 100.0, filters) == 0.0
