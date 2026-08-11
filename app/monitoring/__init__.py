"""Monitoring: alerts and realtime streams."""

from app.monitoring.alerts import AlertService, ConsoleAlertService, FileAlertService

__all__ = [
    "AlertService",
    "ConsoleAlertService",
    "FileAlertService",
    "MarketDataMonitor",
    "StreamHealth",
]


def __getattr__(name: str):
    """Lazy exports to avoid circular imports with RiskManager."""
    if name in {"MarketDataMonitor", "StreamHealth"}:
        from app.monitoring import websocket as ws

        return getattr(ws, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
