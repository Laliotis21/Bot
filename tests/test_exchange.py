"""Exchange adapter and reconciliation tests."""

from __future__ import annotations

from app.config import Settings
from app.core.enums import OrderType, Side
from app.execution import ReconciliationService
from app.portfolio import PositionManager
from tests.fakes import FakeExchange


def test_fake_exchange_filters() -> None:
    ex = FakeExchange()
    f = ex.get_symbol_filters("BTC/USDT")
    assert f.min_notional == 10.0
    assert f.step_size == 0.0001


def test_reconciliation_detects_mismatch() -> None:
    ex = FakeExchange()
    positions = PositionManager()
    positions.open_position(
        symbol="BTC/USDT",
        side=Side.BUY,
        quantity=1.0,
        entry_price=100.0,
    )
    # Exchange free BTC is 0
    report = ReconciliationService(ex, positions).reconcile("BTC/USDT")
    assert report.ok is False
    assert any("exceeds exchange" in m for m in report.mismatches)
