"""Persistence interfaces and SQLite implementation."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from app.core.models import CircuitBreakerState, utc_now


class PersistenceStore(ABC):
    """Abstract persistence layer (SQLite now, Postgres later)."""

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def save_circuit_breaker(self, state: CircuitBreakerState) -> None: ...

    @abstractmethod
    def load_circuit_breaker(self) -> CircuitBreakerState | None: ...

    @abstractmethod
    def save_kv(self, key: str, value: dict[str, Any]) -> None: ...

    @abstractmethod
    def load_kv(self, key: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def insert_json(self, table: str, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    def fetch_json(self, table: str, limit: int = 100) -> list[dict[str, Any]]: ...


class SQLiteStore(PersistenceStore):
    """SQLite-backed persistence for trading state."""

    def __init__(self, database_url: str = "sqlite:///data/trading.db") -> None:
        self.database_url = database_url
        self.path = self._resolve_path(database_url)

    @staticmethod
    def _resolve_path(database_url: str) -> Path:
        if database_url.startswith("sqlite:///"):
            raw = database_url.replace("sqlite:///", "", 1)
            return Path(raw)
        parsed = urlparse(database_url)
        if parsed.scheme in {"", "sqlite"}:
            return Path(parsed.path or "data/trading.db")
        raise ValueError(f"Unsupported database URL for SQLiteStore: {database_url}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuit_breaker (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_statistics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def save_circuit_breaker(self, state: CircuitBreakerState) -> None:
        payload = state.model_dump(mode="json")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO circuit_breaker (id, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (json.dumps(payload), utc_now().isoformat()),
            )

    def load_circuit_breaker(self) -> CircuitBreakerState | None:
        with self._conn() as conn:
            row = conn.execute("SELECT payload FROM circuit_breaker WHERE id = 1").fetchone()
        if not row:
            return None
        return CircuitBreakerState.model_validate_json(row["payload"])

    def save_kv(self, key: str, value: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO kv_store (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, json.dumps(value), utc_now().isoformat()),
            )

    def load_kv(self, key: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(row["value"])

    def insert_json(self, table: str, payload: dict[str, Any]) -> None:
        allowed = {
            "trades",
            "orders",
            "executions",
            "positions",
            "account_snapshots",
            "risk_events",
            "strategy_signals",
            "daily_statistics",
        }
        if table not in allowed:
            raise ValueError(f"Unsupported table: {table}")
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO {table} (payload, created_at) VALUES (?, ?)",
                (json.dumps(payload), utc_now().isoformat()),
            )

    def fetch_json(self, table: str, limit: int = 100) -> list[dict[str, Any]]:
        allowed = {
            "trades",
            "orders",
            "executions",
            "positions",
            "account_snapshots",
            "risk_events",
            "strategy_signals",
            "daily_statistics",
        }
        if table not in allowed:
            raise ValueError(f"Unsupported table: {table}")
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {table} ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]


def create_store(database_url: str = "sqlite:///data/trading.db") -> PersistenceStore:
    """Factory for persistence backends."""
    if database_url.startswith("sqlite"):
        store = SQLiteStore(database_url)
        store.initialize()
        return store
    raise ValueError(
        f"Unsupported database_url '{database_url}'. "
        "Use sqlite:///... for now; Postgres migration is planned."
    )
