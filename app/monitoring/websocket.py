"""WebSocket / real-time market & user-data monitoring."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.risk.manager import RiskManager
from app.utils.logging import get_logger

logger = get_logger("trading.ws")


@dataclass
class StreamHealth:
    last_message_at: datetime | None = None
    connected: bool = False
    reconnect_count: int = 0
    stale: bool = False


class MarketDataMonitor:
    """Track freshness of market data and signal staleness to RiskManager.

    Uses a watchdog thread. Actual exchange WS wiring is injected via
    ``connect_fn`` so tests can fake streams without network I/O.
    """

    def __init__(
        self,
        risk: RiskManager,
        *,
        stale_after_seconds: float = 120.0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.risk = risk
        self.stale_after_seconds = stale_after_seconds
        self.on_event = on_event
        self.health = StreamHealth()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connect_fn: Callable[[Callable[[dict[str, Any]], None]], None] | None = None

    def set_connect_fn(self, fn: Callable[[Callable[[dict[str, Any]], None]], None]) -> None:
        self._connect_fn = fn

    def on_message(self, message: dict[str, Any]) -> None:
        self.health.last_message_at = datetime.now(timezone.utc)
        self.health.connected = True
        if self.health.stale:
            self.health.stale = False
            self.risk.set_market_data_stale(False)
            logger.info("Market data recovered")
        if self.on_event:
            self.on_event(message)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ws-monitor", daemon=True)
        self._thread.start()
        logger.info("WebSocket monitor started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.health.connected = False
        logger.info("WebSocket monitor stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._connect_fn:
                    self.health.connected = True
                    self._connect_fn(self.on_message)
                else:
                    # Idle heartbeat loop when no connector injected
                    time.sleep(0.5)
                if self._stop.is_set():
                    break
                # If connect_fn returns, treat as disconnect
                self.health.connected = False
                self.health.reconnect_count += 1
                logger.warning(
                    "Stream disconnected — reconnecting (count=%s)",
                    self.health.reconnect_count,
                )
                time.sleep(min(2 ** min(self.health.reconnect_count, 5), 30))
            except Exception as exc:
                self.health.connected = False
                self.health.reconnect_count += 1
                logger.error("Stream error: %s — reconnecting", type(exc).__name__)
                time.sleep(min(2 ** min(self.health.reconnect_count, 5), 30))
            finally:
                self._check_stale()

    def _check_stale(self) -> None:
        if self.health.last_message_at is None:
            return
        age = (datetime.now(timezone.utc) - self.health.last_message_at).total_seconds()
        if age > self.stale_after_seconds:
            if not self.health.stale:
                self.health.stale = True
                self.risk.set_market_data_stale(True)
                logger.critical("Market data stale (age=%.1fs) — blocking new entries", age)

    def poll_watchdog(self) -> None:
        """Callable from main loop to detect staleness without dedicated thread."""
        self._check_stale()
