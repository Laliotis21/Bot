"""Execution engine tests with FakeExchange."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.enums import OrderStatus, OrderType, Side
from app.core.models import Fill
from app.execution import ExecutionEngine, ExecutionError
from app.risk import RiskManager
from tests.fakes import FakeExchange


def _stack(paper: bool = True):
    settings = Settings(_env_file=None, paper_trading=True, initial_capital=500)
    risk = RiskManager(settings)
    ex = FakeExchange()
    eng = ExecutionEngine(ex, settings, risk, paper=paper)
    return settings, risk, ex, eng


def test_successful_paper_order() -> None:
    _, _, _, eng = _stack(paper=True)
    order = eng.submit(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=0.01,
        price=100.0,
    )
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == 0.01


def test_duplicate_order_protection() -> None:
    _, _, _, eng = _stack()
    eng.submit(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=0.01,
        price=100.0,
        client_order_id="same",
    )
    with pytest.raises(ExecutionError):
        eng.submit(
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=0.01,
            price=100.0,
            client_order_id="same",
        )


def test_live_timeout_reconciles_before_retry() -> None:
    _, _, ex, eng = _stack(paper=False)
    ex.fail_next = True  # first attempt times out; second succeeds
    order = eng.submit(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=0.01,
        price=100.0,
        max_retries=3,
    )
    assert order.exchange_order_id is not None
    assert order.status in {OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}


def test_rejected_order() -> None:
    _, _, ex, eng = _stack(paper=False)
    ex.reject_next = True
    ex.fail_next = False
    # Make all retries reject
    original = ex.create_order

    def always_reject(*a, **k):
        raise Exception("insufficient balance")

    ex.create_order = always_reject  # type: ignore[method-assign]
    with pytest.raises(ExecutionError):
        eng.submit(
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=0.01,
            price=100.0,
            max_retries=2,
        )
    ex.create_order = original  # type: ignore[method-assign]


def test_partial_fill() -> None:
    _, _, _, eng = _stack(paper=True)
    order = eng.submit(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
    )
    # Simulate remaining partial manually
    order.status = OrderStatus.PARTIALLY_FILLED
    order.filled_quantity = 0.4
    fill = Fill(
        order_id=order.order_id,
        symbol=order.symbol,
        side=Side.BUY,
        quantity=0.6,
        price=100.0,
        fee_amount=0.01,
    )
    eng.apply_fill(order, fill)
    assert order.status == OrderStatus.FILLED
    assert abs(order.filled_quantity - 1.0) < 1e-9
