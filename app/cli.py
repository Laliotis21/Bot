"""Command-line interface for the spot trading framework."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app import STARTUP_WARNING, __version__
from app.backtesting import BacktestEngine, WalkForwardTester
from app.config import LIVE_CONFIRMATION_PHRASE, Settings, load_settings
from app.core.models import ExchangeFilters
from app.monte_carlo import MonteCarloConfig, MonteCarloEngine
from app.strategies import EmaRsiStrategy
from app.utils.logging import setup_logging
from app.utils.reporting import save_backtest_report, save_monte_carlo_report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Modular Binance Spot algorithmic trading framework",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--symbol", default=None)
        p.add_argument("--timeframe", default=None)
        p.add_argument("--start", default=None, help="ISO date start")
        p.add_argument("--end", default=None, help="ISO date end")
        p.add_argument("--capital", type=float, default=None)
        p.add_argument("--data", default=None, help="CSV path with OHLCV")
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--simulations", type=int, default=None)

    p_bt = sub.add_parser("backtest", help="Run historical backtest")
    add_common(p_bt)

    p_mc = sub.add_parser("monte-carlo", help="Run Monte Carlo simulation")
    add_common(p_mc)
    p_mc.add_argument("--mode", choices=["theoretical", "historical"], default="theoretical")

    p_wf = sub.add_parser("walk-forward", help="Run walk-forward test")
    add_common(p_wf)

    p_paper = sub.add_parser("paper", help="Start paper trading mode")
    add_common(p_paper)

    p_live = sub.add_parser("live", help="Start live trading (requires confirmation)")
    add_common(p_live)

    p_report = sub.add_parser("report", help="Summarize latest reports")
    add_common(p_report)

    p_rec = sub.add_parser("reconcile", help="Reconcile local vs exchange state")
    add_common(p_rec)

    return parser.parse_args(argv)


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict = {"_env_file": ".env"}
    # Build from env then overlay CLI — use model_copy for safety
    base = Settings()
    data = base.model_dump()
    if args.symbol:
        data["symbol"] = args.symbol
    if args.timeframe:
        data["timeframe"] = args.timeframe
    if args.capital is not None:
        data["initial_capital"] = args.capital
    if getattr(args, "seed", None) is not None:
        data["random_seed"] = args.seed
    if getattr(args, "simulations", None) is not None:
        data["mc_simulations"] = args.simulations
    # Always force paper unless live command
    if args.command != "live":
        data["paper_trading"] = True
        data["live_trading_confirmation"] = ""
    return Settings(**{k: v for k, v in data.items() if k != "_env_file"}, _env_file=None)


def _load_ohlcv(path: str | None, settings: Settings) -> pd.DataFrame:
    if path:
        df = pd.read_csv(path)
        if "timestamp" in df.columns:
            ts = df["timestamp"]
            if pd.api.types.is_numeric_dtype(ts):
                unit = "ms" if ts.iloc[0] > 10_000_000_000 else "s"
                df["timestamp"] = pd.to_datetime(ts, unit=unit, utc=True)
            else:
                df["timestamp"] = pd.to_datetime(ts, utc=True)
            df = df.set_index("timestamp")
        return df
    # Synthetic demo data for offline runs without network
    idx = pd.date_range("2024-01-01", periods=800, freq="h", tz="UTC")
    import numpy as np

    rng = np.random.default_rng(settings.random_seed or 42)
    rets = rng.normal(0, 0.002, size=len(idx))
    close = 100 * np.cumprod(1 + rets)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": rng.uniform(1, 10, size=len(idx)),
        },
        index=idx,
    )
    return df


def cmd_backtest(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    setup_logging(settings.log_level, settings.logs_dir)
    print(STARTUP_WARNING)
    strategy = EmaRsiStrategy.from_settings(settings)
    data = _load_ohlcv(args.data, settings)
    engine = BacktestEngine(strategy, settings)
    result = engine.run(data)
    paths = save_backtest_report(result, settings.reports_dir)
    print(json.dumps(result.metrics.to_dict(), indent=2))
    print(f"Reports written: {paths}")
    return 0


def cmd_monte_carlo(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    setup_logging(settings.log_level, settings.logs_dir)
    print(STARTUP_WARNING)
    cfg = MonteCarloConfig(
        simulations=settings.mc_simulations,
        trades_per_sim=settings.mc_trades_per_sim,
        initial_capital=settings.initial_capital,
        risk_per_trade=settings.risk_per_trade,
        win_probability=settings.target_win_rate,
        win_r=settings.target_rr,
        fee_rate=settings.effective_taker_fee,
        slippage_rate=settings.slippage_rate,
        random_seed=settings.random_seed,
        ruin_threshold=settings.ruin_threshold,
    )
    engine = MonteCarloEngine(cfg)
    if args.mode == "historical":
        # Run a backtest first to obtain trade distribution
        strategy = EmaRsiStrategy.from_settings(settings)
        bt = BacktestEngine(strategy, settings).run(_load_ohlcv(args.data, settings))
        if not bt.trades:
            print("No backtest trades available; falling back to theoretical mode", file=sys.stderr)
            result = engine.run_theoretical()
        else:
            result = engine.run_historical(bt.trades)
    else:
        result = engine.run_theoretical()
    paths = save_monte_carlo_report(result, settings.reports_dir)
    print(json.dumps(result.to_dict(), indent=2))
    print(f"Reports written: {paths}")
    return 0


def cmd_walk_forward(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    setup_logging(settings.log_level, settings.logs_dir)
    print(STARTUP_WARNING)
    strategy = EmaRsiStrategy.from_settings(settings)
    data = _load_ohlcv(args.data, settings)
    tester = WalkForwardTester(
        strategy,
        settings,
        train_bars=300,
        validation_bars=100,
        test_bars=100,
        step_bars=100,
    )
    report = tester.run(data)
    out = Path(settings.reports_dir) / "walk_forward_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    print(json.dumps({"in_sample": report.in_sample, "out_of_sample": report.out_of_sample, "walk_forward": report.walk_forward}, indent=2))
    print(f"Wrote {out}")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    settings = settings.model_copy(update={"paper_trading": True})
    setup_logging(settings.log_level, settings.logs_dir)
    print(STARTUP_WARNING)
    print("Paper trading mode ready. Use the PaperTradingEngine API or provide --data for a replay.")
    if args.data:
        from app.exchanges.binance import BinanceAdapter
        from app.paper import PaperTradingEngine

        # Use adapter only for interface; do not place live orders
        class _NullExchange(BinanceAdapter):
            def __init__(self) -> None:  # noqa: D401
                self.settings = settings
                self.name = "paper-null"
                self.client = None

            def fetch_balance(self):
                return {"free": {settings.currency: settings.initial_capital}}

            def fetch_ohlcv(self, *a, **k):
                return []

            def fetch_ticker(self, symbol):
                return {"last": 0}

            def fetch_open_orders(self, symbol=None):
                return []

            def fetch_positions(self, symbol=None):
                return []

            def create_order(self, *a, **k):
                raise RuntimeError("Paper null exchange does not create live orders")

            def cancel_order(self, *a, **k):
                return {}

            def cancel_all_orders(self, symbol=None):
                return []

            def fetch_order(self, *a, **k):
                return {}

            def load_markets(self):
                return {}

            def get_symbol_filters(self, symbol):
                return ExchangeFilters(symbol=symbol, min_qty=0.0001, step_size=0.0001, min_notional=10)

        engine = PaperTradingEngine(settings, EmaRsiStrategy.from_settings(settings), _NullExchange())
        df = _load_ohlcv(args.data, settings)
        for i in range(60, len(df)):
            engine.on_market_data(df.iloc[: i + 1])
        engine.shutdown()
        print(json.dumps(engine.ledger.summary(), indent=2))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    print(STARTUP_WARNING)
    try:
        settings = Settings()
    except Exception as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 2
    if settings.paper_trading:
        print(
            "Refusing live mode: PAPER_TRADING is true. "
            f"Set PAPER_TRADING=false and LIVE_TRADING_CONFIRMATION={LIVE_CONFIRMATION_PHRASE}",
            file=sys.stderr,
        )
        return 2
    from app.exchanges import create_exchange
    from app.execution.live import build_live_stack

    exchange = create_exchange(settings)
    stack = build_live_stack(settings, EmaRsiStrategy.from_settings(settings), exchange)
    print("Live stack initialized. Reconciliation complete. Exiting without order loop in CLI scaffold.")
    stack["alerts"].send("Bot stopped (LIVE TRADING CLI scaffold)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    reports = Path(settings.reports_dir)
    files = sorted(reports.glob("*")) if reports.exists() else []
    print(json.dumps([str(f) for f in files], indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    print(STARTUP_WARNING)
    settings = Settings()
    if settings.paper_trading and not settings.binance_api_key:
        print("No API keys configured; reconcile skipped in paper/offline mode.")
        return 0
    from app.exchanges import create_exchange
    from app.execution import ReconciliationService
    from app.portfolio import PositionManager

    exchange = create_exchange(settings)
    report = ReconciliationService(exchange, PositionManager()).reconcile(settings.symbol)
    print(json.dumps({"ok": report.ok, "mismatches": report.mismatches, "actions": report.actions_taken}, indent=2))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    handlers = {
        "backtest": cmd_backtest,
        "monte-carlo": cmd_monte_carlo,
        "walk-forward": cmd_walk_forward,
        "paper": cmd_paper,
        "live": cmd_live,
        "report": cmd_report,
        "reconcile": cmd_reconcile,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
