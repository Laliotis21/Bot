"""Strongly typed configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THE_RISK"


class Settings(BaseSettings):
    """Validated trading framework settings.

    All trading parameters are loaded from environment variables or a ``.env``
    file. Invalid values are rejected at startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Capital & risk
    initial_capital: float = Field(default=500.0, gt=0)
    risk_per_trade: float = Field(default=0.015, gt=0, le=1.0)
    target_rr: float = Field(default=2.0, gt=0)
    target_win_rate: float = Field(default=0.55, gt=0, lt=1.0)
    max_drawdown: float = Field(default=0.15, gt=0, le=1.0)
    max_daily_loss: float = Field(default=0.04, gt=0, le=1.0)
    max_portfolio_exposure: float = Field(default=1.0, gt=0, le=1.0)
    max_open_positions: int = Field(default=3, ge=1)
    max_position_size: float = Field(default=1.0, gt=0, le=1.0)
    max_consecutive_losses: int = Field(default=5, ge=1)
    max_trades_per_day: int = Field(default=20, ge=1)
    emergency_close_on_drawdown: bool = True

    # Fees & slippage
    maker_fee: float = Field(default=0.00075, ge=0)
    taker_fee: float = Field(default=0.00075, ge=0)
    bnb_discount: float = Field(default=0.0, ge=0, le=1.0)
    slippage_bps: float = Field(default=5.0, ge=0)

    # Exchange
    exchange: Literal["binance", "binanceus"] = "binance"
    currency: str = "USDT"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    binance_api_key: str = ""
    binance_secret_key: str = ""

    # Modes
    paper_trading: bool = True
    live_trading_confirmation: str = ""

    # Strategy defaults (EMA + RSI)
    ema_fast: int = Field(default=20, ge=1)
    ema_slow: int = Field(default=50, ge=2)
    rsi_period: int = Field(default=14, ge=2)
    rsi_oversold: float = Field(default=30.0, ge=0, le=100)
    rsi_overbought: float = Field(default=70.0, ge=0, le=100)

    # Monte Carlo
    mc_simulations: int = Field(default=1000, ge=1)
    mc_trades_per_sim: int = Field(default=500, ge=1)
    random_seed: int | None = 42
    ruin_threshold: float = Field(default=0.5, gt=0, le=1.0)

    # Persistence / paths
    database_url: str = "sqlite:///data/trading.db"
    reports_dir: str = "reports"
    logs_dir: str = "logs"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Market-data freshness (seconds)
    stale_data_seconds: float = Field(default=120.0, gt=0)
    api_error_threshold: int = Field(default=5, ge=1)

    # Accounting
    cost_basis_method: Literal["fifo"] = "fifo"

    @field_validator("ema_slow")
    @classmethod
    def slow_must_exceed_fast(cls, v: int, info) -> int:  # type: ignore[no-untyped-def]
        fast = info.data.get("ema_fast")
        if fast is not None and v <= fast:
            raise ValueError("ema_slow must be greater than ema_fast")
        return v

    @field_validator("rsi_overbought")
    @classmethod
    def overbought_above_oversold(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        oversold = info.data.get("rsi_oversold")
        if oversold is not None and v <= oversold:
            raise ValueError("rsi_overbought must be greater than rsi_oversold")
        return v

    @model_validator(mode="after")
    def validate_live_mode(self) -> Settings:
        if not self.paper_trading:
            if self.live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
                raise ValueError(
                    "Live trading requires LIVE_TRADING_CONFIRMATION="
                    f"{LIVE_CONFIRMATION_PHRASE}. Default mode is paper trading."
                )
            if not self.binance_api_key or not self.binance_secret_key:
                raise ValueError("Live trading requires BINANCE_API_KEY and BINANCE_SECRET_KEY")
        return self

    @property
    def effective_taker_fee(self) -> float:
        """Taker fee after optional BNB discount."""
        return self.taker_fee * (1.0 - self.bnb_discount)

    @property
    def effective_maker_fee(self) -> float:
        """Maker fee after optional BNB discount."""
        return self.maker_fee * (1.0 - self.bnb_discount)

    @property
    def slippage_rate(self) -> float:
        """Slippage as a decimal rate (bps / 10_000)."""
        return self.slippage_bps / 10_000.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


def load_settings(*, reload: bool = False) -> Settings:
    """Load settings, optionally clearing the cache."""
    if reload:
        get_settings.cache_clear()
    return get_settings()
