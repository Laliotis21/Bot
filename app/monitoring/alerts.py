"""Alerting interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger("trading.alerts")


class AlertService(ABC):
    """Abstract alert channel (Telegram/Discord/email can be added later)."""

    @abstractmethod
    def send(self, message: str) -> None:
        """Send an alert message."""


class ConsoleAlertService(AlertService):
    """Log alerts to console/logger."""

    def send(self, message: str) -> None:
        logger.critical("ALERT: %s", message)


class FileAlertService(AlertService):
    """Append alerts to a file and log them."""

    def __init__(self, path: str | Path = "logs/alerts.log") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, message: str) -> None:
        logger.critical("ALERT: %s", message)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")


class MultiplexAlertService(AlertService):
    """Fan-out to multiple alert backends."""

    def __init__(self, services: list[AlertService]) -> None:
        self.services = services

    def send(self, message: str) -> None:
        for svc in self.services:
            svc.send(message)
