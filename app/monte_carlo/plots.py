"""Monte Carlo visualizations saved under reports/."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from app.monte_carlo.engine import MonteCarloResult


def plot_equity_paths(result: MonteCarloResult, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "monte_carlo_paths.png"

    paths = result.equity_paths
    x = np.arange(paths.shape[1])
    fig, ax = plt.subplots(figsize=(10, 6))
    for i in range(paths.shape[0]):
        ax.plot(x, paths[i], color="steelblue", alpha=0.03, linewidth=0.8)
    p5 = np.percentile(paths, 5, axis=0)
    p50 = np.percentile(paths, 50, axis=0)
    p95 = np.percentile(paths, 95, axis=0)
    ax.plot(x, p50, color="black", linewidth=2, label="median")
    ax.plot(x, p5, color="crimson", linewidth=1.5, label="5th pct")
    ax.plot(x, p95, color="green", linewidth=1.5, label="95th pct")
    ax.set_title("Monte Carlo Equity Paths")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Equity")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_drawdown_distribution(result: MonteCarloResult, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "drawdown_distribution.png"
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(result.max_drawdowns, bins=40, color="salmon", edgecolor="white")
    ax.set_title("Maximum Drawdown Distribution")
    ax.set_xlabel("Max Drawdown")
    ax.set_ylabel("Frequency")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_final_balance_distribution(result: MonteCarloResult, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "final_balance_distribution.png"
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(result.final_balances, bins=40, color="seagreen", edgecolor="white")
    ax.set_title("Final Balance Distribution")
    ax.set_xlabel("Final Balance")
    ax.set_ylabel("Frequency")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def save_all_charts(result: MonteCarloResult, out_dir: str | Path) -> dict[str, str]:
    return {
        "paths": str(plot_equity_paths(result, out_dir)),
        "drawdown": str(plot_drawdown_distribution(result, out_dir)),
        "final_balance": str(plot_final_balance_distribution(result, out_dir)),
    }
