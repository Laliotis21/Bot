"""Monte Carlo simulation engine (independent module)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from app.core.models import TradeRecord


Mode = Literal["historical", "theoretical"]


@dataclass
class MonteCarloConfig:
    simulations: int = 1000
    trades_per_sim: int = 500
    initial_capital: float = 500.0
    risk_per_trade: float = 0.015
    win_probability: float = 0.55
    win_r: float = 2.0
    loss_r: float = 1.0
    fee_rate: float = 0.00075
    slippage_rate: float = 0.0005
    random_seed: int | None = 42
    ruin_threshold: float = 0.5
    drawdown_thresholds: tuple[float, ...] = (0.25, 0.50)
    balance_thresholds: tuple[float, ...] = (250.0, 100.0)


@dataclass
class MonteCarloResult:
    mode: Mode
    config: MonteCarloConfig
    final_balances: np.ndarray
    max_drawdowns: np.ndarray
    equity_paths: np.ndarray  # shape (simulations, trades+1)
    median_final_balance: float = 0.0
    p5_final_balance: float = 0.0
    p95_final_balance: float = 0.0
    mean_final_balance: float = 0.0
    std_final_balance: float = 0.0
    median_max_drawdown: float = 0.0
    p5_max_drawdown: float = 0.0
    p95_max_drawdown: float = 0.0
    prob_drawdown: dict[str, float] = field(default_factory=dict)
    prob_below_threshold: dict[str, float] = field(default_factory=dict)
    probability_of_ruin: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "simulations": self.config.simulations,
            "trades_per_sim": self.config.trades_per_sim,
            "random_seed": self.config.random_seed,
            "median_final_balance": self.median_final_balance,
            "p5_final_balance": self.p5_final_balance,
            "p95_final_balance": self.p95_final_balance,
            "mean_final_balance": self.mean_final_balance,
            "std_final_balance": self.std_final_balance,
            "median_max_drawdown": self.median_max_drawdown,
            "p5_max_drawdown": self.p5_max_drawdown,
            "p95_max_drawdown": self.p95_max_drawdown,
            "prob_drawdown": self.prob_drawdown,
            "prob_below_threshold": self.prob_below_threshold,
            "probability_of_ruin": self.probability_of_ruin,
            "disclaimer": (
                "Monte Carlo results are probabilistic illustrations, "
                "not forecasts of profitability."
            ),
        }


class MonteCarloEngine:
    """Compounding Monte Carlo with fees/slippage.

    Modes:
    - historical: resample empirical R-multiples / net returns from backtest trades
    - theoretical: Bernoulli win/loss with configured R multiples (not guaranteed)
    """

    def __init__(self, config: MonteCarloConfig | None = None) -> None:
        self.config = config or MonteCarloConfig()

    def run_theoretical(self) -> MonteCarloResult:
        rng = np.random.default_rng(self.config.random_seed)
        cfg = self.config
        n_sim, n_tr = cfg.simulations, cfg.trades_per_sim
        paths = np.zeros((n_sim, n_tr + 1), dtype=float)
        paths[:, 0] = cfg.initial_capital
        max_dd = np.zeros(n_sim, dtype=float)

        for s in range(n_sim):
            equity = cfg.initial_capital
            peak = equity
            worst_dd = 0.0
            for t in range(n_tr):
                risk_amount = equity * cfg.risk_per_trade
                win = rng.random() < cfg.win_probability
                # Apply fees/slippage as a drag on R outcome
                cost_r = (cfg.fee_rate + cfg.slippage_rate) / max(cfg.risk_per_trade, 1e-12)
                if win:
                    r = cfg.win_r - cost_r
                else:
                    r = -(cfg.loss_r + cost_r)
                equity = max(0.0, equity + risk_amount * r)
                paths[s, t + 1] = equity
                peak = max(peak, equity)
                dd = (peak - equity) / peak if peak > 0 else 0.0
                worst_dd = max(worst_dd, dd)
                if equity <= cfg.initial_capital * cfg.ruin_threshold:
                    paths[s, t + 1 :] = equity
                    break
            max_dd[s] = worst_dd

        return self._summarize("theoretical", paths, max_dd)

    def run_historical(self, trades: list[TradeRecord]) -> MonteCarloResult:
        if not trades:
            raise ValueError("Historical Monte Carlo requires backtest trades")
        rng = np.random.default_rng(self.config.random_seed)
        cfg = self.config

        # Use actual net R-multiples when available; else net_pnl / risk proxy
        samples: list[float] = []
        for tr in trades:
            if tr.r_multiple is not None:
                samples.append(float(tr.r_multiple))
            else:
                risk = abs(tr.entry_price - (tr.stop_loss or tr.entry_price)) * tr.quantity
                samples.append(float(tr.net_pnl / risk) if risk > 0 else 0.0)
        sample_arr = np.array(samples, dtype=float)

        n_sim, n_tr = cfg.simulations, cfg.trades_per_sim
        paths = np.zeros((n_sim, n_tr + 1), dtype=float)
        paths[:, 0] = cfg.initial_capital
        max_dd = np.zeros(n_sim, dtype=float)

        for s in range(n_sim):
            equity = cfg.initial_capital
            peak = equity
            worst_dd = 0.0
            draws = rng.choice(sample_arr, size=n_tr, replace=True)
            for t, r in enumerate(draws):
                risk_amount = equity * cfg.risk_per_trade
                # Historical R already includes fees/slippage from backtest;
                # optionally apply an extra small drag if configured (default already in trades)
                equity = max(0.0, equity + risk_amount * float(r))
                paths[s, t + 1] = equity
                peak = max(peak, equity)
                dd = (peak - equity) / peak if peak > 0 else 0.0
                worst_dd = max(worst_dd, dd)
                if equity <= cfg.initial_capital * cfg.ruin_threshold:
                    paths[s, t + 1 :] = equity
                    break
            max_dd[s] = worst_dd

        return self._summarize("historical", paths, max_dd)

    def _summarize(self, mode: Mode, paths: np.ndarray, max_dd: np.ndarray) -> MonteCarloResult:
        finals = paths[:, -1]
        cfg = self.config
        result = MonteCarloResult(
            mode=mode,
            config=cfg,
            final_balances=finals,
            max_drawdowns=max_dd,
            equity_paths=paths,
            median_final_balance=float(np.median(finals)),
            p5_final_balance=float(np.percentile(finals, 5)),
            p95_final_balance=float(np.percentile(finals, 95)),
            mean_final_balance=float(np.mean(finals)),
            std_final_balance=float(np.std(finals)),
            median_max_drawdown=float(np.median(max_dd)),
            p5_max_drawdown=float(np.percentile(max_dd, 5)),
            p95_max_drawdown=float(np.percentile(max_dd, 95)),
            prob_drawdown={
                f"{int(th * 100)}pct": float(np.mean(max_dd >= th)) for th in cfg.drawdown_thresholds
            },
            prob_below_threshold={
                str(th): float(np.mean(finals < th)) for th in cfg.balance_thresholds
            },
            probability_of_ruin=float(np.mean(finals <= cfg.initial_capital * cfg.ruin_threshold)),
        )
        return result
