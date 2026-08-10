"""CLI smoke tests."""

from __future__ import annotations

from app.cli import main


def test_cli_backtest_and_monte_carlo(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # Minimal env isolation
    (tmp_path / "reports").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "data").mkdir()
    assert main(["backtest", "--capital", "500", "--seed", "1"]) == 0
    assert (tmp_path / "reports" / "backtest_report.json").exists()
    assert main(["monte-carlo", "--simulations", "20", "--seed", "2", "--mode", "theoretical"]) == 0
    assert (tmp_path / "reports" / "monte_carlo_report.json").exists()


def test_cli_live_refuses_without_confirmation(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    # Default settings are paper
    code = main(["live"])
    assert code == 2
