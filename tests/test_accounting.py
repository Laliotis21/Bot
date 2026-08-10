"""Accounting / FIFO ledger tests."""

from __future__ import annotations

from app.accounting import AccountingLedger
from app.core.enums import Side


def test_buy_not_realized_and_fifo_sell(tmp_path) -> None:
    ledger = AccountingLedger(exchange="binance")
    ledger.cash_balance = 1000.0
    buy1 = ledger.record_buy(
        symbol="BTC/USDT", quantity=1, price=100, fee_amount=1, order_id="o1"
    )
    assert buy1.net_realized_pnl is None
    assert buy1.side == Side.BUY
    ledger.record_buy(symbol="BTC/USDT", quantity=1, price=110, fee_amount=1, order_id="o2")
    sell = ledger.record_sell(
        symbol="BTC/USDT", quantity=1, price=120, fee_amount=1, order_id="o3"
    )
    # FIFO cost basis from first lot (~101 including fee)
    assert sell.cost_basis is not None
    assert abs(sell.cost_basis - 101.0) < 1e-9
    assert sell.net_realized_pnl is not None
    assert sell.gross_pnl is not None
    path = ledger.export_csv(tmp_path / "ledger.csv")
    text = path.read_text(encoding="utf-8")
    assert "net_realized_pnl" in text
    assert "BUY" in text and "SELL" in text
