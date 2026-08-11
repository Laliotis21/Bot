"""Risk management and position sizing tests."""

from __future__ import annotations

from app.config import Settings
from app.core import ExchangeFilters, Signal, SignalType
from app.persistence import create_store
from app.risk import RiskManager, compute_position_size
from app.risk.manager import example_sizing_from_spec


def _settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        paper_trading=True,
        initial_capital=500.0,
        risk_per_trade=0.015,
        max_drawdown=0.15,
        max_daily_loss=0.04,
        max_consecutive_losses=3,
        max_trades_per_day=5,
    )
    base.update(kwargs)
    return Settings(**base)


def test_position_sizing_spec_example() -> None:
    result = example_sizing_from_spec()
    assert abs(result.quantity - 1.5) < 1e-9
    assert abs(result.risk_amount - 7.5) < 1e-9


def test_dynamic_compounding_increases_size() -> None:
    filters = ExchangeFilters(symbol="BTC/USDT", min_qty=0.0, step_size=0.0001, min_notional=1.0)
    small = compute_position_size(
        equity=500, risk_pct=0.015, entry_price=100, stop_loss=95,
        filters=filters, available_balance=500,
    )
    large = compute_position_size(
        equity=1000, risk_pct=0.015, entry_price=100, stop_loss=95,
        filters=filters, available_balance=1000,
    )
    assert large.quantity > small.quantity


def test_drawdown_circuit_breaker(tmp_path) -> None:
    store = create_store(f"sqlite:///{tmp_path / 't.db'}")
    rm = RiskManager(_settings(), store=store, equity=1000.0)
    assert rm.circuit_tripped is False
    rm.update_equity(840.0)  # 16% DD from 1000
    assert rm.circuit_tripped is True
    assert rm.circuit_requires_reset is True

    # Peak / tripped state survives restart
    rm2 = RiskManager(_settings(), store=store, equity=840.0)
    assert rm2.circuit_tripped is True
    assert rm2.peak_equity == 1000.0

    decision = rm2.evaluate_entry(
        Signal(signal_type=SignalType.BUY, symbol="BTC/USDT", entry_price=100, stop_loss=95),
        ExchangeFilters(symbol="BTC/USDT", min_qty=0.001, step_size=0.001, min_notional=10),
    )
    assert decision.allowed is False
    assert decision.reason == "circuit_breaker"

    rm2.reset_circuit_breaker(explicit=True)
    assert rm2.circuit_tripped is False


def test_daily_loss_guard() -> None:
    rm = RiskManager(_settings(max_daily_loss=0.04), equity=1000.0)
    rm.daily_start_equity = 1000.0
    rm.update_equity(950.0)  # -5%
    assert rm.daily_entries_blocked_until is not None
    decision = rm.evaluate_entry(
        Signal(signal_type=SignalType.BUY, symbol="ETH/USDT", entry_price=100, stop_loss=95),
        ExchangeFilters(symbol="ETH/USDT", min_qty=0.001, step_size=0.001, min_notional=10),
    )
    assert decision.allowed is False
    assert decision.reason == "daily_loss_guard"


def test_filters_reject_tiny_order() -> None:
    rm = RiskManager(_settings(), equity=500.0)
    decision = rm.evaluate_entry(
        Signal(signal_type=SignalType.BUY, symbol="BTC/USDT", entry_price=100, stop_loss=99.999),
        ExchangeFilters(symbol="BTC/USDT", min_qty=1.0, step_size=1.0, min_notional=1000.0),
    )
    assert decision.allowed is False
