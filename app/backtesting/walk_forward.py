"""Walk-forward / out-of-sample testing.

Parameters must never be optimized on the TEST window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.backtesting.engine import BacktestEngine, BacktestResult
from app.backtesting.metrics import BacktestMetrics
from app.config.settings import Settings
from app.core.models import ExchangeFilters
from app.strategies.base import Strategy


@dataclass
class WalkForwardFold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    validation_start: pd.Timestamp
    validation_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train: BacktestResult | None = None
    validation: BacktestResult | None = None
    test: BacktestResult | None = None


@dataclass
class WalkForwardReport:
    folds: list[WalkForwardFold]
    in_sample: dict[str, Any]
    out_of_sample: dict[str, Any]
    walk_forward: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "in_sample": self.in_sample,
            "out_of_sample": self.out_of_sample,
            "walk_forward": self.walk_forward,
            "folds": [
                {
                    "train": [str(f.train_start), str(f.train_end)],
                    "validation": [str(f.validation_start), str(f.validation_end)],
                    "test": [str(f.test_start), str(f.test_end)],
                    "train_metrics": f.train.metrics.to_dict() if f.train else None,
                    "validation_metrics": f.validation.metrics.to_dict() if f.validation else None,
                    "test_metrics": f.test.metrics.to_dict() if f.test else None,
                }
                for f in self.folds
            ],
        }


class WalkForwardTester:
    """Rolling TRAIN → VALIDATION → TEST windows.

    This module evaluates a fixed strategy configuration. It does not search
    parameters on the test set (anti-overfitting).
    """

    def __init__(
        self,
        strategy: Strategy,
        settings: Settings,
        filters: ExchangeFilters | None = None,
        *,
        train_bars: int = 500,
        validation_bars: int = 100,
        test_bars: int = 100,
        step_bars: int = 100,
    ) -> None:
        self.strategy = strategy
        self.settings = settings
        self.filters = filters
        self.train_bars = train_bars
        self.validation_bars = validation_bars
        self.test_bars = test_bars
        self.step_bars = step_bars

    def run(self, data: pd.DataFrame) -> WalkForwardReport:
        engine = BacktestEngine(self.strategy, self.settings, self.filters)
        df = engine._normalize(data)
        n = len(df)
        need = self.train_bars + self.validation_bars + self.test_bars
        if n < need:
            raise ValueError(f"Need at least {need} bars for walk-forward, got {n}")

        folds: list[WalkForwardFold] = []
        start = 0
        while start + need <= n:
            tr_end = start + self.train_bars
            va_end = tr_end + self.validation_bars
            te_end = va_end + self.test_bars
            fold = WalkForwardFold(
                train_start=df.index[start],
                train_end=df.index[tr_end - 1],
                validation_start=df.index[tr_end],
                validation_end=df.index[va_end - 1],
                test_start=df.index[va_end],
                test_end=df.index[te_end - 1],
            )
            # Evaluate each segment independently (fixed params — no optimize on test)
            fold.train = engine.run(df.iloc[start:tr_end])
            fold.validation = engine.run(df.iloc[tr_end:va_end])
            fold.test = engine.run(df.iloc[va_end:te_end])
            folds.append(fold)
            start += self.step_bars

        def _avg(metric_key: str, results: list[BacktestResult]) -> float:
            vals = [getattr(r.metrics, metric_key) for r in results]
            return float(sum(vals) / len(vals)) if vals else 0.0

        trains = [f.train for f in folds if f.train]
        vals = [f.validation for f in folds if f.validation]
        tests = [f.test for f in folds if f.test]

        in_sample = {
            "avg_total_return": _avg("total_return", trains),
            "avg_win_rate": _avg("win_rate", trains),
            "avg_max_drawdown": _avg("maximum_drawdown", trains),
            "folds": len(trains),
            "note": "TRAIN windows only — not a guarantee of future performance",
        }
        oos = {
            "avg_total_return": _avg("total_return", vals),
            "avg_win_rate": _avg("win_rate", vals),
            "avg_max_drawdown": _avg("maximum_drawdown", vals),
            "folds": len(vals),
            "note": "VALIDATION windows — used for model selection, not final claim",
        }
        wf = {
            "avg_total_return": _avg("total_return", tests),
            "avg_win_rate": _avg("win_rate", tests),
            "avg_max_drawdown": _avg("maximum_drawdown", tests),
            "folds": len(tests),
            "note": "TEST windows — untouched by parameter optimization",
        }
        return WalkForwardReport(folds, in_sample, oos, wf)
