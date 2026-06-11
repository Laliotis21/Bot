#!/usr/bin/env python3
"""
Backtest της ίδιας στρατηγικής (EMA 9/21 daily crossover, SL/TP brackets)
σε ΜΕΤΟΧΕΣ / ΔΕΙΚΤΕΣ / FOREX / ΕΜΠΟΡΕΥΜΑΤΑ, με δεδομένα Yahoo Finance.

Ο πυρήνας προσομοίωσης είναι ο ίδιος με το crypto backtest (simulate από το
backtest.py) — αλλάζει μόνο η πηγή δεδομένων και τα κόστη συναλλαγών.

ΧΡΗΣΗ
    python3 backtest_tradfi.py --tickers SPY --sl 0.02 --tp 0.06
    python3 backtest_tradfi.py --tickers "EURUSD=X" --sl 0.01 --tp 0.03 --fee 0.0001
    python3 backtest_tradfi.py --tickers NVDA,TSLA --sl 0.05 --tp 0.15 --long-only
"""

from __future__ import annotations

import argparse
import json

import yfinance as yf

from backtest import simulate, print_summary


def fetch_yahoo(tickers: list[str], since: str) -> dict[str, list[list]]:
    """Κατεβάζει daily OHLCV από Yahoo Finance σε μορφή rows [ts, o, h, l, c, v]."""
    sym_rows: dict[str, list[list]] = {}
    for t in tickers:
        df = yf.download(t, start=since, interval="1d", progress=False,
                         auto_adjust=True, multi_level_index=False)
        if df.empty:
            raise SystemExit(f"Δεν βρέθηκαν δεδομένα για '{t}' στο Yahoo Finance.")
        rows = []
        for ts, row in df.iterrows():
            if row[["Open", "High", "Low", "Close"]].isna().any():
                continue
            rows.append([
                int(ts.timestamp() * 1000),
                float(row["Open"]), float(row["High"]),
                float(row["Low"]), float(row["Close"]),
                float(row.get("Volume", 0) or 0),
            ])
        sym_rows[t] = rows
        print(f"   {t}: {len(rows)} κεριά (από {rows[0] and df.index[0].date()})")
    return sym_rows


def main() -> None:
    ap = argparse.ArgumentParser(description="EMA 9/21 backtest σε μετοχές/forex (Yahoo).")
    ap.add_argument("--tickers", required=True,
                    help="Yahoo tickers με κόμμα (π.χ. SPY,QQQ ή EURUSD=X)")
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--sl", type=float, default=0.02)
    ap.add_argument("--tp", type=float, default=0.06)
    ap.add_argument("--fee", type=float, default=0.0002,
                    help="κόστος ανά σκέλος: ~0.0002 μετοχές, ~0.0001 forex (spread)")
    ap.add_argument("--trend-ema", type=int, default=0)
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",")]
    print(f"Λήψη daily δεδομένων από {args.since} (Yahoo Finance)...")
    sym_rows = fetch_yahoo(tickers, args.since)

    r = simulate(sym_rows, tickers, args.capital, args.sl, args.tp, args.fee,
                 trend_ema=args.trend_ema, long_only=args.long_only)
    print_summary(r, ",".join(tickers))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(f"\nΑναλυτικά: {args.out}")


if __name__ == "__main__":
    main()
