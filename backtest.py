#!/usr/bin/env python3
"""
Backtest της στρατηγικής του trading_bot.py σε πραγματικά ιστορικά δεδομένα.

Προσομοιώνει ΑΚΡΙΒΩΣ τους κανόνες του bot:
  - EMA 9/21 crossover σε ΚΛΕΙΣΜΕΝΑ κεριά (ίδια λογική με detect_signal:
    ίδιο compute_emas, σήμα μόνο σε φρέσκια διασταύρωση, ελάχιστο ιστορικό).
  - Είσοδος στο open του επόμενου κεριού (το bot μπαίνει αμέσως μετά το κλείσιμο).
  - First-Come-First-Served: μία θέση τη φορά, σταθερή σειρά σάρωσης.
  - Compounding: notional = 95% του τρέχοντος κεφαλαίου, 1x.
  - Έξοδος ΜΟΝΟ με SL/TP (intrabar μέσω high/low, συντηρητικά: αν χτυπηθούν
    και τα δύο στο ίδιο κερί, μετράμε το SL).
  - Fees ανά σκέλος (default: taker 0.05% crypto futures).

Ο πυρήνας (simulate) είναι ανεξάρτητος πηγής δεδομένων — δέχεται OHLCV rows
[ts_ms, open, high, low, close, volume] ανά σύμβολο. Το CLI εδώ τραβάει
crypto δεδομένα από Binance USDⓂ futures μέσω ccxt (χωρίς keys)· για
μετοχές/forex δες backtest_tradfi.py.

ΧΡΗΣΗ
    python3 backtest.py                                  # 1d, SL 5% / TP 15%
    python3 backtest.py --since 2026-01-01 --capital 1000
    python3 backtest.py --timeframe 4h --sl 0.025 --tp 0.075
    python3 backtest.py --symbols "BTC/USDT:USDT" --trend-ema 200
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from trading_bot import compute_emas

DEFAULT_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
EMA_FAST, EMA_SLOW = 9, 21
POSITION_PCT = 0.95
FEE_PCT = 0.0005  # taker 0.05% ανά σκέλος (crypto futures)


@dataclass
class Trade:
    symbol: str
    side: str
    entry_date: str
    entry: float
    exit_date: str
    exit: float
    reason: str          # "tp" | "sl" | "open" (ανοιχτή στο τέλος)
    pnl: float           # σε νόμισμα αναφοράς, μετά τα fees
    pnl_pct: float       # επί του notional
    equity_after: float
    days_held: float


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def simulate(sym_rows: dict[str, list[list]], symbols: list[str], capital: float,
             sl_pct: float, tp_pct: float, fee_pct: float = FEE_PCT,
             trend_ema: int = 0, long_only: bool = False) -> dict:
    """Τρέχει την προσομοίωση πάνω σε έτοιμα OHLCV rows ανά σύμβολο."""
    sym_idx: dict[str, dict[int, int]] = {}
    sym_ef: dict[str, list[float]] = {}
    sym_es: dict[str, list[float]] = {}
    sym_etrend: dict[str, list[float]] = {}
    for s in symbols:
        rows = sym_rows[s]
        sym_idx[s] = {r[0]: i for i, r in enumerate(rows)}
        # Προϋπολογισμός EMA: ο EMA (adjust=False) στο i εξαρτάται ΜΟΝΟ από
        # δεδομένα έως το i — ταυτίζεται με υπολογισμό σε prefix.
        closes_all = [r[4] for r in rows]
        ef, es = compute_emas(closes_all, EMA_FAST, EMA_SLOW)
        sym_ef[s], sym_es[s] = ef.tolist(), es.tolist()
        if trend_ema:
            et, _ = compute_emas(closes_all, trend_ema, trend_ema)
            sym_etrend[s] = et.tolist()

    all_ts = sorted({t for d in sym_idx.values() for t in d})

    equity = capital
    peak = capital
    max_dd = 0.0
    position: dict | None = None
    last_acted: dict[str, int] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[str, float]] = []

    for i, ts in enumerate(all_ts):
        date = _fmt_ts(ts)

        # --- Διαχείριση ανοιχτής θέσης: έλεγχος SL/TP στο κερί ---
        if position is not None and ts in sym_idx[position["symbol"]]:
            c = sym_rows[position["symbol"]][sym_idx[position["symbol"]][ts]]
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
                fees = p["notional"] * fee_pct + p["notional"] * (1 + move) * fee_pct
                pnl = gross - fees
                equity += pnl
                days = (ts - p["entry_ts"]) / 86_400_000
                trades.append(Trade(
                    p["symbol"], p["side"], p["entry_date"], p["entry"],
                    date, exit_price, reason, round(pnl, 2),
                    round(move - fee_pct * 2, 4), round(equity, 2), round(days, 2),
                ))
                position = None

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        equity_curve.append((date, round(equity, 2)))

        # --- Σάρωση για σήμα (μόνο FLAT), είσοδος στο open του ΕΠΟΜΕΝΟΥ κεριού ---
        if position is None and i + 1 < len(all_ts):
            for symbol in symbols:  # FCFS με σταθερή σειρά
                j = sym_idx[symbol].get(ts)
                if j is None or last_acted.get(symbol) == ts:
                    continue
                if j < EMA_SLOW + 1:  # όπως το len(closes) < slow + 2 του bot
                    continue
                ef, es = sym_ef[symbol], sym_es[symbol]
                if ef[j - 1] <= es[j - 1] and ef[j] > es[j]:
                    signal = "long"
                elif ef[j - 1] >= es[j - 1] and ef[j] < es[j]:
                    signal = "short"
                else:
                    continue
                if long_only and signal == "short":
                    continue
                if trend_ema:
                    close_j = sym_rows[symbol][j][4]
                    et = sym_etrend[symbol][j]
                    if (signal == "long" and close_j <= et) or \
                       (signal == "short" and close_j >= et):
                        continue  # σήμα κόντρα στο καθεστώς τάσης -> απόρριψη
                next_ts = all_ts[i + 1]
                k = sym_idx[symbol].get(next_ts)
                if k is None:
                    continue
                entry = sym_rows[symbol][k][1]  # open επόμενου κεριού
                notional = equity * POSITION_PCT
                sl = entry * (1 - sl_pct) if signal == "long" else entry * (1 + sl_pct)
                tp = entry * (1 + tp_pct) if signal == "long" else entry * (1 - tp_pct)
                position = {
                    "symbol": symbol, "side": signal, "entry": entry,
                    "sl": sl, "tp": tp, "notional": notional,
                    "entry_ts": next_ts, "entry_date": _fmt_ts(next_ts),
                }
                last_acted[symbol] = ts
                break  # FCFS: σταματάμε τη σάρωση

    # Ανοιχτή θέση στο τέλος: αποτίμηση mark-to-market στο τελευταίο close.
    if position is not None:
        rows = sym_rows[position["symbol"]]
        last_ts, last_close = rows[-1][0], rows[-1][4]
        move = (last_close - position["entry"]) / position["entry"]
        if position["side"] == "short":
            move = -move
        pnl = position["notional"] * move - position["notional"] * fee_pct
        equity += pnl
        trades.append(Trade(
            position["symbol"], position["side"], position["entry_date"], position["entry"],
            "ανοιχτή", last_close, "open", round(pnl, 2),
            round(move - fee_pct, 4), round(equity, 2),
            round((last_ts - position["entry_ts"]) / 86_400_000, 2),
        ))

    # --- Buy & hold σύγκριση (πρώτο σύμβολο της λίστας) ---
    bh_rows = sym_rows[symbols[0]]
    bh_start, bh_end = bh_rows[0][4], bh_rows[-1][4]

    closed = [t for t in trades if t.reason in ("tp", "sl")]
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    years = max((all_ts[-1] - all_ts[0]) / (365.25 * 86_400_000), 1e-9)

    return {
        "params": {"sl_pct": sl_pct, "tp_pct": tp_pct, "fee_pct_per_side": fee_pct,
                   "trend_ema": trend_ema, "long_only": long_only},
        "period": {"from": _fmt_ts(all_ts[0]), "to": equity_curve[-1][0],
                   "years": round(years, 2)},
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
        "avg_days_held": round(sum(t.days_held for t in closed) / len(closed), 2) if closed else None,
        "buy_hold_first_symbol_pct": round((bh_end / bh_start - 1) * 100, 1),
        "per_symbol": {
            s: {
                "trades": len([t for t in closed if t.symbol == s]),
                "wins": len([t for t in closed if t.symbol == s and t.pnl > 0]),
                "pnl": round(sum(t.pnl for t in closed if t.symbol == s), 2),
            } for s in symbols
        },
        "per_side": {
            side: {
                "trades": len([t for t in closed if t.side == side]),
                "wins": len([t for t in closed if t.side == side and t.pnl > 0]),
                "pnl": round(sum(t.pnl for t in closed if t.side == side), 2),
            } for side in ("long", "short")
        },
        "trades": [asdict(t) for t in trades],
        "equity_curve": equity_curve[:: max(1, len(equity_curve) // 2000)],
    }


def print_summary(r: dict, title: str) -> None:
    p = r["params"]
    extras = ""
    if p["long_only"]:
        extras += ", long-only"
    if p["trend_ema"]:
        extras += f", trend EMA{p['trend_ema']}"
    print(f"\n=== ΑΠΟΤΕΛΕΣΜΑΤΑ BACKTEST ({title}, SL {p['sl_pct']*100:g}% / TP {p['tp_pct']*100:g}%{extras}) ===")
    print(f"Περίοδος           : {r['period']['from']} -> {r['period']['to']} ({r['period']['years']} χρόνια)")
    print(f"Κεφάλαιο           : {r['capital_start']:,.0f} -> {r['capital_end']:,.2f}")
    print(f"Συνολική απόδοση   : {r['total_return_pct']}%  (CAGR {r['cagr_pct']}%/έτος)")
    print(f"Μέγιστο drawdown   : -{r['max_drawdown_pct']}%")
    print(f"Trades (κλεισμένα) : {r['trades_closed']}  |  Νίκες {r['wins']} / Ήττες {r['losses']}"
          f"  |  Win rate {r['win_rate_pct']}%")
    print(f"Profit factor      : {r['profit_factor']}  |  Μέση διάρκεια {r['avg_days_held']} ημέρες")
    print(f"Buy & Hold (1ο σύμβολο): {r['buy_hold_first_symbol_pct']}% (ίδια περίοδος)")
    print("Ανά σύμβολο:")
    for s, d in r["per_symbol"].items():
        print(f"   {s:<16} trades={d['trades']:<4} wins={d['wins']:<4} pnl={d['pnl']:+,.2f}")


def fetch_crypto(symbols: list[str], timeframe: str, since: str) -> dict[str, list[list]]:
    import ccxt
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since_ms = int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() * 1000)
    sym_rows: dict[str, list[list]] = {}
    print(f"Λήψη ιστορικών {timeframe} δεδομένων από {since} (Binance USDⓂ futures)...")
    for s in symbols:
        out: list[list] = []
        cursor = since_ms
        while True:
            batch = ex.fetch_ohlcv(s, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            out.extend(b for b in batch if not out or b[0] > out[-1][0])
            if len(batch) < 1000:
                break
            cursor = batch[-1][0] + 1
            time.sleep(ex.rateLimit / 1000)
        sym_rows[s] = out
        print(f"   {s}: {len(out)} κεριά (από {_fmt_ts(out[0][0])})")
    return sym_rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest της στρατηγικής EMA 9/21 του bot (crypto).")
    ap.add_argument("--since", default="2020-09-01", help="ημερομηνία έναρξης (YYYY-MM-DD)")
    ap.add_argument("--capital", type=float, default=10_000.0, help="αρχικό κεφάλαιο USDT")
    ap.add_argument("--timeframe", default="1d", help="timeframe κεριών (π.χ. 1d, 4h)")
    ap.add_argument("--sl", type=float, default=0.05, help="Stop Loss ποσοστό (π.χ. 0.05)")
    ap.add_argument("--tp", type=float, default=0.15, help="Take Profit ποσοστό (π.χ. 0.15)")
    ap.add_argument("--fee", type=float, default=FEE_PCT, help="fee ανά σκέλος (π.χ. 0.0005)")
    ap.add_argument("--out", default="backtest_results.json", help="αρχείο αποτελεσμάτων")
    ap.add_argument("--symbols", default=None,
                    help="λίστα συμβόλων χωρισμένα με κόμμα (π.χ. BTC/USDT:USDT)")
    ap.add_argument("--trend-ema", type=int, default=0,
                    help="regime filter: long μόνο πάνω/short μόνο κάτω από EMA(N), 0=off")
    ap.add_argument("--long-only", action="store_true", help="μόνο long θέσεις")
    args = ap.parse_args()

    symbols = ([s.strip() for s in args.symbols.split(",")] if args.symbols
               else list(DEFAULT_SYMBOLS))
    sym_rows = fetch_crypto(symbols, args.timeframe, args.since)
    r = simulate(sym_rows, symbols, args.capital, args.sl, args.tp, args.fee,
                 trend_ema=args.trend_ema, long_only=args.long_only)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    print_summary(r, args.timeframe)
    print(f"\nΑναλυτικά: {args.out} (trades + equity curve)")


if __name__ == "__main__":
    main()
