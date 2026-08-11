"""FIFO tax/accounting ledger.

BUY does not create realized PnL. Realized PnL occurs on disposing lots.
CSV export is an audit aid — not tax advice or a complete filing.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.enums import Side
from app.core.models import LedgerEntry, utc_now


@dataclass
class Lot:
    quantity: float
    cost_per_unit: float
    opened_at: datetime
    fee_allocated: float = 0.0


@dataclass
class AccountingLedger:
    """Transaction-level ledger with FIFO cost basis."""

    exchange: str = "binance"
    method: str = "fifo"
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    entries: list[LedgerEntry] = field(default_factory=list)
    cash_balance: float = 0.0

    def record_buy(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float,
        fee_amount: float,
        fee_currency: str = "USDT",
        order_id: str,
        trade_id: str | None = None,
        client_order_id: str | None = None,
        position_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> LedgerEntry:
        ts = timestamp or utc_now()
        quote = quantity * price
        # Cost basis includes entry fee
        cost_per_unit = (quote + fee_amount) / quantity if quantity else price
        self.lots.setdefault(symbol, []).append(
            Lot(quantity=quantity, cost_per_unit=cost_per_unit, opened_at=ts, fee_allocated=fee_amount)
        )
        self.cash_balance -= quote + fee_amount
        entry = LedgerEntry(
            timestamp_utc=ts,
            exchange=self.exchange,
            order_id=order_id,
            trade_id=trade_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            executed_price=price,
            quote_amount=quote,
            fee_amount=fee_amount,
            fee_currency=fee_currency,
            gross_pnl=None,
            cost_basis=quantity * cost_per_unit,
            net_realized_pnl=None,  # BUY is not realized PnL
            account_balance=self.cash_balance,
            position_id=position_id,
        )
        self.entries.append(entry)
        return entry

    def record_sell(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float,
        fee_amount: float,
        fee_currency: str = "USDT",
        order_id: str,
        trade_id: str | None = None,
        client_order_id: str | None = None,
        position_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> LedgerEntry:
        ts = timestamp or utc_now()
        quote = quantity * price
        cost_basis, _ = self._consume_fifo(symbol, quantity)
        gross = quote - cost_basis
        net = gross - fee_amount
        self.cash_balance += quote - fee_amount
        entry = LedgerEntry(
            timestamp_utc=ts,
            exchange=self.exchange,
            order_id=order_id,
            trade_id=trade_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=Side.SELL,
            quantity=quantity,
            executed_price=price,
            quote_amount=quote,
            fee_amount=fee_amount,
            fee_currency=fee_currency,
            gross_pnl=gross,
            cost_basis=cost_basis,
            net_realized_pnl=net,
            account_balance=self.cash_balance,
            position_id=position_id,
        )
        self.entries.append(entry)
        return entry

    def _consume_fifo(self, symbol: str, quantity: float) -> tuple[float, float]:
        remaining = quantity
        cost = 0.0
        fees = 0.0
        queue = self.lots.setdefault(symbol, [])
        while remaining > 1e-12:
            if not queue:
                raise ValueError(f"FIFO: insufficient lots for {symbol}")
            lot = queue[0]
            take = min(lot.quantity, remaining)
            ratio = take / lot.quantity if lot.quantity else 0.0
            cost += take * lot.cost_per_unit
            fees += lot.fee_allocated * ratio
            lot.quantity -= take
            lot.fee_allocated -= lot.fee_allocated * ratio
            remaining -= take
            if lot.quantity <= 1e-12:
                queue.pop(0)
        return cost, fees

    def export_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "timestamp_utc",
            "exchange",
            "order_id",
            "trade_id",
            "client_order_id",
            "symbol",
            "side",
            "quantity",
            "executed_price",
            "quote_amount",
            "fee_amount",
            "fee_currency",
            "gross_pnl",
            "cost_basis",
            "net_realized_pnl",
            "account_balance",
            "position_id",
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for e in self.entries:
                row = e.model_dump(mode="json")
                row["side"] = e.side.value
                writer.writerow({k: row.get(k) for k in fields})
        return path

    def summary(self) -> dict[str, Any]:
        realized = [e.net_realized_pnl for e in self.entries if e.net_realized_pnl is not None]
        return {
            "method": self.method,
            "entries": len(self.entries),
            "realized_pnl_total": float(sum(realized)),
            "cash_balance": self.cash_balance,
            "open_lots": {sym: sum(l.quantity for l in lots) for sym, lots in self.lots.items()},
            "disclaimer": "Not tax advice. Verify with a qualified professional.",
        }
