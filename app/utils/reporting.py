"""Report generation (JSON/HTML/PNG)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from app.backtesting.engine import BacktestResult
from app.monte_carlo.engine import MonteCarloResult
from app.monte_carlo.plots import save_all_charts


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _write_html(path: Path, title: str, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: Georgia, serif; margin: 2rem; background: #f7f5f2; color: #1a1a1a; }}
h1 {{ font-size: 1.6rem; }}
pre {{ background: #fff; padding: 1rem; border: 1px solid #ddd; overflow: auto; }}
img {{ max-width: 100%; margin: 1rem 0; }}
.note {{ color: #555; font-size: 0.9rem; }}
</style></head><body>
<h1>{title}</h1>
<p class="note">Past performance does not guarantee future results. Not financial advice.</p>
{body}
</body></html>"""
    path.write_text(html, encoding="utf-8")
    return path


def save_backtest_report(result: BacktestResult, out_dir: str | Path) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    json_path = _write_json(out / "backtest_report.json", payload)

    # Equity + drawdown charts
    eq_path = out / "equity_curve.png"
    dd_path = out / "drawdown_curve.png"
    fig, ax = plt.subplots(figsize=(10, 5))
    result.equity_curve.plot(ax=ax, color="#1f4e79")
    ax.set_title("Equity Curve")
    ax.set_ylabel("Equity")
    fig.tight_layout()
    fig.savefig(eq_path, dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    result.drawdown_curve.plot(ax=ax, color="#8b0000")
    ax.set_title("Drawdown Curve")
    ax.set_ylabel("Drawdown")
    fig.tight_layout()
    fig.savefig(dd_path, dpi=120)
    plt.close(fig)

    metrics_pre = json.dumps(result.metrics.to_dict(), indent=2)
    html_path = _write_html(
        out / "backtest_report.html",
        "Backtest Report",
        f"<pre>{metrics_pre}</pre>"
        f'<img src="equity_curve.png" alt="equity"/><img src="drawdown_curve.png" alt="dd"/>',
    )
    return {
        "json": str(json_path),
        "html": str(html_path),
        "equity": str(eq_path),
        "drawdown": str(dd_path),
    }


def save_monte_carlo_report(result: MonteCarloResult, out_dir: str | Path) -> dict[str, str]:
    out = Path(out_dir)
    charts = save_all_charts(result, out)
    json_path = _write_json(out / "monte_carlo_report.json", result.to_dict())
    body = f"<pre>{json.dumps(result.to_dict(), indent=2)}</pre>"
    for name, p in charts.items():
        body += f'<h2>{name}</h2><img src="{Path(p).name}" alt="{name}"/>'
    html_path = _write_html(out / "monte_carlo_report.html", "Monte Carlo Report", body)
    return {"json": str(json_path), "html": str(html_path), **charts}
