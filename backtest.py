#!/usr/bin/env python3
"""
Backtest της στρατηγικής του trading_bot.py σε πραγματικά ιστορικά δεδομένα.

Προσομοιώνει ΑΚΡΙΒΩΣ τους κανόνες του bot:
  - EMA 9/21 crossover σε ΚΛΕΙΣΜΕΝΑ daily κεριά (ίδιο detect_signal).
  - Είσοδος στο open του επόμενου κεριού (το bot μπαίνει αμέσως μετά το κλείσιμο).
  - First-Come-First-Served: μία θέση τη φορά, σάρωση BTC -> ETH -> SOL.
  - Compounding: notional = 95% του τρέχοντος κεφαλαίου, 1x.
  - Έξοδος ΜΟΝΟ με SL -5% / TP +15% (intrabar μέσω high/low, συντηρητικά:
    αν χτυπηθούν και τα δύο στο ίδιο κερί, μετράμε το SL).
  - Fees: taker 0.05% ανά σκέλος (είσοδος + έξοδος).

Δεδομένα: δημόσιο API Binance USDⓂ futures μέσω ccxt (χωρίς keys).

ΧΡΗΣΗ
    python3 backtest.py                       # πλήρες ιστορικό (από το 2020)
    python3 backtest.py --since 2023-01-01    # συγκεκριμένη περίοδος
    python3 backtest.py --capital 10000
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import ccxt

from trading_bot import compute_emas, detect_signal

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
EMA_FAST, EMA_SLOW = 9, 21
SL_PCT, TP_PCT = 0.05, 0.15
POSITION_PCT = 0.95
FEE_PCT = 0.0005  # taker 0.05% ανά σκέλος


@dataclass
class Trade:
    symbol: str
    side: str
    entry_date: str
    entry: float
    exit_date: str
    exit: float
    reason: str          # "tp" | "sl" | "open" (ανοιχτή στο τέλος)
    pnl: float           # σε USDT, μετά τα fees
    pnl_pct: float       # επί του notional
    equity_after: float
    days_held: int


def fetch_daily(ex: ccxt.Exchange, symbol: str, since_ms: int) -> list[list]:
    """Κατεβάζει όλο το διαθέσιμο daily OHLCV με pagination."""
    out: list[list] = []
    cursor = since_ms
    while True:
        batch = ex.fetch_ohlcv(symbol, "1d", since=cursor, limit=1000)
        if not batch:
            break
        out.extend(b for b in batch if not out or b[0] > out[-1][0])
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + 1
        time.sleep(ex.rateLimit / 1000)
    return out


def run_backtest(since: str, capital: float) -> dict:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since_ms = int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() * 1000)

    print(f"Λήψη ιστορικών daily δεδομένων από {since} (Binance USDⓂ futures)...")
    data: dict[str, dict[int, list]] = {}
    for s in SYMBOLS:
        rows = fetch_daily(ex, s, since_ms)
        data[s] = {r[0]: r for r in rows}
        first = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc).date()
        print(f"   {s}: {len(rows)} κεριά (από {first})")

    # Κοινός άξονας χρόνου: όλα τα timestamps, ταξινομημένα.
    all_ts = sorted({t for d in data.values() for t in d})

    equity = capital
    peak = capital
    max_dd = 0.0
    position: dict | None = None
    last_acted: dict[str, int] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[str, float]] = []

    def closes_until(symbol: str, ts: int) -> list[float]:
        return [data[symbol][t][4] for t in all_ts if t <= ts and t in data[symbol]]

    for i, ts in enumerate(all_ts):
        date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()

        # --- Διαχείριση ανοιχτής θέσης: έλεγχος SL/TP στο κερί της ημέρας ---
        if position is not None and ts in data[position["symbol"]]:
            c = data[position["symbol"]][ts]
            high, low = c[2], c[3]
            p = position
            exit_price = reason = None
            if p["side"] == "long":
                if low <= p["sl"]:
                    exit_price, reason = p["sl"], "sl"      # συντηρητικά: SL πρώτο
                elif high >= p["tp"]:
                    exit_price, reason = p["tp"], "tp"
            else:
                if high >= p["sl"]:
                    exit_price, reason = p["sl"], "sl"
                elif low <= p["tp"]:
                    exit_price, reason = p["tp"], "tp"
            if exit_price is not None:
                move = (exit_price - p["entry"]) / p["entry"]
                if p["side"] == "short":
                    move = -move
                gross = p["notional"] * move
                fees = p["notional"] * FEE_PCT + p["notional"] * (1 + move) * FEE_PCT
                pnl = gross - fees
                equity += pnl
                days = (ts - p["entry_ts"]) // 86_400_000
                trades.append(Trade(
                    p["symbol"], p["side"], p["entry_date"], p["entry"],
                    date, exit_price, reason, round(pnl, 2),
                    round(move - FEE_PCT * 2, 4), round(equity, 2), int(days),
                ))
                position = None

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        equity_curve.append((date, round(equity, 2)))

        # --- Σάρωση για σήμα (μόνο FLAT), είσοδος στο open του ΕΠΟΜΕΝΟΥ κεριού ---
        if position is None and i + 1 < len(all_ts):
            for symbol in SYMBOLS:  # FCFS με σταθερή σειρά
                if ts not in data[symbol] or last_acted.get(symbol) == ts:
                    continue
                closes = closes_until(symbol, ts)
                signal = detect_signal(closes, EMA_FAST, EMA_SLOW)
                if signal is None:
                    continue
                next_ts = all_ts[i + 1]
                if next_ts not in data[symbol]:
                    continue
                entry = data[symbol][next_ts][1]  # open επόμενης ημέρας
                notional = equity * POSITION_PCT
                sl = entry * (1 - SL_PCT) if signal == "long" else entry * (1 + SL_PCT)
                tp = entry * (1 + TP_PCT) if signal == "long" else entry * (1 - TP_PCT)
                position = {
                    "symbol": symbol, "side": signal, "entry": entry,
                    "sl": sl, "tp": tp, "notional": notional,
                    "entry_ts": next_ts,
                    "entry_date": datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc).date().isoformat(),
                }
                last_acted[symbol] = ts
                break  # FCFS: σταματάμε τη σάρωση

    # Ανοιχτή θέση στο τέλος: αποτίμηση mark-to-market στο τελευταίο close.
    if position is not None:
        last_ts = max(t for t in all_ts if t in data[position["symbol"]])
        last_close = data[position["symbol"]][last_ts][4]
        move = (last_close - position["entry"]) / position["entry"]
        if position["side"] == "short":
            move = -move
        pnl = position["notional"] * move - position["notional"] * FEE_PCT
        equity += pnl
        trades.append(Trade(
            position["symbol"], position["side"], position["entry_date"], position["entry"],
            "ανοιχτή", last_close, "open", round(pnl, 2),
            round(move - FEE_PCT, 4), round(equity, 2),
            int((last_ts - position["entry_ts"]) // 86_400_000),
        ))

    # --- Buy & hold σύγκριση (BTC) ---
    btc_ts = sorted(data["BTC/USDT:USDT"])
    bh_start = data["BTC/USDT:USDT"][btc_ts[0]][4]
    bh_end = data["BTC/USDT:USDT"][btc_ts[-1]][4]

    closed = [t for t in trades if t.reason in ("tp", "sl")]
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    years = max((all_ts[-1] - all_ts[0]) / (365.25 * 86_400_000), 1e-9)

    return {
        "period": {"from": since, "to": equity_curve[-1][0], "years": round(years, 2)},
        "capital_start": capital,
        "capital_end": round(equity, 2),
        "total_return_pct": round((equity / capital - 1) * 100, 1),
        "cagr_pct": round(((equity / capital) ** (1 / years) - 1) * 100, 1),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "trades_closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_days_held": round(sum(t.days_held for t in closed) / len(closed), 1) if closed else None,
        "buy_hold_btc_return_pct": round((bh_end / bh_start - 1) * 100, 1),
        "per_symbol": {
            s: {
                "trades": len([t for t in closed if t.symbol == s]),
                "wins": len([t for t in closed if t.symbol == s and t.pnl > 0]),
                "pnl": round(sum(t.pnl for t in closed if t.symbol == s), 2),
            } for s in SYMBOLS
        },
        "per_side": {
            side: {
                "trades": len([t for t in closed if t.side == side]),
                "wins": len([t for t in closed if t.side == side and t.pnl > 0]),
                "pnl": round(sum(t.pnl for t in closed if t.side == side), 2),
            } for side in ("long", "short")
        },
        "trades": [asdict(t) for t in trades],
        "equity_curve": equity_curve,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest της στρατηγικής EMA 9/21 του bot.")
    ap.add_argument("--since", default="2020-09-01", help="ημερομηνία έναρξης (YYYY-MM-DD)")
    ap.add_argument("--capital", type=float, default=10_000.0, help="αρχικό κεφάλαιο USDT")
    ap.add_argument("--out", default="backtest_results.json", help="αρχείο αποτελεσμάτων")
    args = ap.parse_args()

    r = run_backtest(args.since, args.capital)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)

    print("\n=== ΑΠΟΤΕΛΕΣΜΑΤΑ BACKTEST (κανόνες ίδιοι με το bot) ===")
    print(f"Περίοδος           : {r['period']['from']} -> {r['period']['to']} ({r['period']['years']} χρόνια)")
    print(f"Κεφάλαιο           : {r['capital_start']:,.0f} -> {r['capital_end']:,.2f} USDT")
    print(f"Συνολική απόδοση   : {r['total_return_pct']}%  (CAGR {r['cagr_pct']}%/έτος)")
    print(f"Μέγιστο drawdown   : -{r['max_drawdown_pct']}%")
    print(f"Trades (κλεισμένα) : {r['trades_closed']}  |  Νίκες {r['wins']} / Ήττες {r['losses']}"
          f"  |  Win rate {r['win_rate_pct']}%")
    print(f"Profit factor      : {r['profit_factor']}  |  Μέση διάρκεια {r['avg_days_held']} ημέρες")
    print(f"Buy & Hold BTC     : {r['buy_hold_btc_return_pct']}% (ίδια περίοδος)")
    print("\nΑνά σύμβολο:")
    for s, d in r["per_symbol"].items():
        print(f"   {s:<16} trades={d['trades']:<3} wins={d['wins']:<3} pnl={d['pnl']:+,.2f} USDT")
    print("Ανά κατεύθυνση:")
    for side, d in r["per_side"].items():
        print(f"   {side:<6} trades={d['trades']:<3} wins={d['wins']:<3} pnl={d['pnl']:+,.2f} USDT")
    print(f"\nΑναλυτικά: {args.out} (trades + equity curve)")


if __name__ == "__main__":
    main()
