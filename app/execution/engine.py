"""Order execution engine with safe retries and duplicate protection."""

from __future__ import annotations

import time
from typing import Any, Callable

from app.accounting.ledger import AccountingLedger
from app.config.settings import Settings
from app.core.enums import OrderStatus, OrderType, Side
from app.core.models import Fill, Order, new_id, utc_now
from app.exchanges.base import ExchangeAdapter
from app.persistence.store import PersistenceStore
from app.risk.manager import RiskManager
from app.utils.logging import get_logger

logger = get_logger("trading.execution")


class ExecutionError(Exception):
    """Raised when an order cannot be safely executed."""


class ExecutionEngine:
    """Places and tracks orders with failure-safe behavior.

    Never blindly retries without reconciling whether the original order
    was accepted. Uses client order IDs for idempotency where supported.
    """

    def __init__(
        self,
        exchange: ExchangeAdapter,
        settings: Settings,
        risk: RiskManager,
        ledger: AccountingLedger | None = None,
        store: PersistenceStore | None = None,
        *,
        paper: bool = True,
        fill_simulator: Callable[[Order], Fill] | None = None,
    ) -> None:
        self.exchange = exchange
        self.settings = settings
        self.risk = risk
        self.ledger = ledger
        self.store = store
        self.paper = paper
        self.fillim = fill_simulator
        self.orders: dict[str, Order] = {}
        self._seen_client_ids: set[str] = set()

    def submit(
        self,
        *,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: float,
        price: float | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
        position_id: str | None = None,
        max_retries: int = 3,
    ) -> Order:
        cid = client_order_id or new_id("cli_")
        if cid in self._seen_client_ids or cid in self.risk.pending_client_order_ids:
            raise ExecutionError(f"Duplicate client order id blocked: {cid}")

        order = Order(
            client_order_id=cid,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            position_id=position_id,
            status=OrderStatus.PENDING,
        )
        self._seen_client_ids.add(cid)
        self.risk.register_client_order_id(cid)
        self.orders[order.order_id] = order
        self._persist_order(order)

        if self.paper:
            return self._paper_fill(order)

        return self._live_submit(order, max_retries=max_retries)

    def cancel(self, order_id: str) -> Order:
        order = self.orders[order_id]
        if order.is_terminal:
            return order
        if not self.paper and order.exchange_order_id:
            try:
                self.exchange.cancel_order(order.exchange_order_id, order.symbol)
            except Exception as exc:
                logger.error("Cancel failed for %s: %s — reconciling", order_id, exc)
                self._reconcile_order(order)
                return order
        order.status = OrderStatus.CANCELED
        order.updated_at = utc_now()
        self.risk.clear_client_order_id(order.client_order_id)
        self._persist_order(order)
        return order

    def cancel_all(self, symbol: str | None = None) -> list[Order]:
        canceled = []
        for order in list(self.orders.values()):
            if order.is_terminal:
                continue
            if symbol and order.symbol != symbol:
                continue
            canceled.append(self.cancel(order.order_id))
        return canceled

    def _paper_fill(self, order: Order) -> Order:
        if self.fillim:
            fill = self.fillim(order)
        else:
            px = order.price or order.stop_price or 0.0
            slip = self.settings.slippage_rate
            if order.side == Side.BUY:
                px = px * (1.0 + slip) if px else 0.0
            else:
                px = px * (1.0 - slip) if px else 0.0
            fee = px * order.quantity * self.settings.effective_taker_fee
            fill = Fill(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=px,
                fee_amount=fee,
                fee_currency=self.settings.currency,
                is_maker=False,
            )
        return self.apply_fill(order, fill)

    def apply_fill(self, order: Order, fill: Fill) -> Order:
        order.filled_quantity += fill.quantity
        if order.average_price is None:
            order.average_price = fill.price
        else:
            prev = order.filled_quantity - fill.quantity
            order.average_price = (
                (order.average_price * prev + fill.price * fill.quantity) / order.filled_quantity
                if order.filled_quantity
                else fill.price
            )
        order.fee_amount += fill.fee_amount
        if order.filled_quantity + 1e-12 >= order.quantity:
            order.status = OrderStatus.FILLED
            self.risk.clear_client_order_id(order.client_order_id)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        order.updated_at = utc_now()
        self._persist_order(order)
        if self.store:
            self.store.insert_json("executions", fill.model_dump(mode="json"))
        if self.ledger and fill.quantity > 0:
            if fill.side == Side.BUY:
                self.ledger.record_buy(
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    price=fill.price,
                    fee_amount=fill.fee_amount,
                    fee_currency=fill.fee_currency,
                    order_id=order.order_id,
                    trade_id=fill.trade_id,
                    client_order_id=order.client_order_id,
                    position_id=order.position_id,
                    timestamp=fill.timestamp,
                )
            else:
                self.ledger.record_sell(
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    price=fill.price,
                    fee_amount=fill.fee_amount,
                    fee_currency=fill.fee_currency,
                    order_id=order.order_id,
                    trade_id=fill.trade_id,
                    client_order_id=order.client_order_id,
                    position_id=order.position_id,
                    timestamp=fill.timestamp,
                )
        logger.info(
            "Fill order=%s side=%s qty=%.8f px=%.8f status=%s",
            order.order_id,
            fill.side.value,
            fill.quantity,
            fill.price,
            order.status.value,
        )
        return order

    def _live_submit(self, order: Order, *, max_retries: int) -> Order:
        params: dict[str, Any] = {"newClientOrderId": order.client_order_id}
        if order.stop_price is not None:
            params["stopPrice"] = order.stop_price
        attempt = 0
        last_exc: Exception | None = None
        while attempt < max_retries:
            attempt += 1
            try:
                raw = self.exchange.create_order(
                    order.symbol,
                    order.side,
                    order.order_type,
                    order.quantity,
                    order.price,
                    params=params,
                )
                order.exchange_order_id = str(raw.get("id") or raw.get("orderId") or "")
                order.status = OrderStatus.OPEN
                order.updated_at = utc_now()
                self._persist_order(order)
                self.risk.clear_api_errors()
                # Sync fill state from exchange response when available
                filled = float(raw.get("filled") or 0.0)
                if filled > 0:
                    avg = float(raw.get("average") or order.price or 0.0)
                    fee = 0.0
                    fees = raw.get("fees") or []
                    if fees and isinstance(fees, list):
                        fee = float(fees[0].get("cost") or 0.0)
                    fill = Fill(
                        order_id=order.order_id,
                        trade_id=str(raw.get("id")),
                        symbol=order.symbol,
                        side=order.side,
                        quantity=filled,
                        price=avg,
                        fee_amount=fee,
                    )
                    self.apply_fill(order, fill)
                return order
            except Exception as exc:
                last_exc = exc
                self.risk.record_api_error()
                logger.error(
                    "Order submit failed attempt=%s client_id=%s err=%s — reconciling before retry",
                    attempt,
                    order.client_order_id,
                    type(exc).__name__,
                )
                # Never blind retry: check if order actually landed
                found = self._find_by_client_id(order)
                if found:
                    order.exchange_order_id = str(found.get("id"))
                    order.status = OrderStatus.OPEN
                    self._persist_order(order)
                    return order
                time.sleep(min(2 ** attempt, 16))

        order.status = OrderStatus.REJECTED
        order.updated_at = utc_now()
        self.risk.clear_client_order_id(order.client_order_id)
        self._persist_order(order)
        raise ExecutionError(f"Order rejected after retries: {last_exc}")

    def _find_by_client_id(self, order: Order) -> dict[str, Any] | None:
        try:
            open_orders = self.exchange.fetch_open_orders(order.symbol)
            for o in open_orders:
                if o.get("clientOrderId") == order.client_order_id:
                    return o
            # Also try fetch_order if exchange id unknown — some exchanges support client id
            return None
        except Exception as exc:
            logger.warning("Reconcile lookup failed: %s", type(exc).__name__)
            return None

    def _reconcile_order(self, order: Order) -> None:
        if not order.exchange_order_id:
            found = self._find_by_client_id(order)
            if found:
                order.exchange_order_id = str(found.get("id"))
        if order.exchange_order_id:
            try:
                raw = self.exchange.fetch_order(order.exchange_order_id, order.symbol)
                status = str(raw.get("status") or "").lower()
                if status in {"canceled", "cancelled"}:
                    order.status = OrderStatus.CANCELED
                elif status == "closed" or status == "filled":
                    order.status = OrderStatus.FILLED
                order.updated_at = utc_now()
            except Exception as exc:
                logger.error("fetch_order during reconcile failed: %s", type(exc).__name__)

    def _persist_order(self, order: Order) -> None:
        if self.store:
            self.store.insert_json("orders", order.model_dump(mode="json"))
