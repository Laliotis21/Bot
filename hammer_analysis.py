#!/usr/bin/env python3
"""
Εμπειρικός έλεγχος: έχει εκμεταλλεύσιμο edge το hammer candlestick;

Ορισμός hammer (κλασικός):
  - κάτω ουρά >= 2x σώμα
  - πάνω ουρά <= 25% του εύρους κεριού
  - εμφανίζεται μετά από πτώση (απόδοση 5 κεριών < 0)
Shooting star: ο καθρέφτης του (για shorts).

Δύο μετρήσεις:
  A) Στατιστική: μέση απόδοση 1/3/5 κεριών ΜΕΤΑ το σήμα vs όλα τα κεριά
     (το «edge» πρέπει να ξεπερνά το baseline, όχι απλώς να είναι θετικό).
  B) Πρακτική: trade με είσοδο στο open του επόμενου, SL κάτω από το low
     του hammer, TP σε 2R, μέγιστη διάρκεια 10 κεριά, fees 0.05%/σκέλος.

ΧΡΗΣΗ
    python3 hammer_analysis.py            # crypto (1d) + μετοχές/χρυσός
"""

from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

FEE = 0.0005
MAX_HOLD = 10


def find_signals(rows: list[list], kind: str) -> list[int]:
    """Επιστρέφει indexes κεριών-σημάτων. kind: 'hammer' ή 'star'."""
    out = []
    for i in range(6, len(rows) - 1):
        o, h, l, c = rows[i][1:5]
        rng = h - l
        if rng <= 0:
            continue
        body = abs(c - o)
        lower = min(o, c) - l
        upper = h - max(o, c)
        trend5 = rows[i][4] / rows[i - 5][4] - 1
        if kind == "hammer":
            if lower >= 2 * body and upper <= 0.25 * rng and trend5 < 0:
                out.append(i)
        else:  # shooting star
            if upper >= 2 * body and lower <= 0.25 * rng and trend5 > 0:
                out.append(i)
    return out


def stat_edge(rows: list[list], signals: list[int], horizon: int, short: bool = False) -> tuple:
    """Μέση απόδοση horizon κεριών μετά το σήμα (από open i+1) vs baseline."""
    sig_rets, all_rets = [], []
    for i in range(6, len(rows) - horizon - 1):
        entry = rows[i + 1][1]
        exit_ = rows[i + 1 + horizon - 1][4]
        if entry <= 0:
            continue
        r = exit_ / entry - 1
        if short:
            r = -r
        all_rets.append(r)
        if i in signals:
            sig_rets.append(r)
    avg = lambda x: 100 * sum(x) / len(x) if x else float("nan")
    return avg(sig_rets), avg(all_rets), len(sig_rets)


def simulate_trades(rows: list[list], signals: list[int], short: bool = False) -> dict:
    """Trade: entry open i+1, SL στο άκρο της ουράς, TP 2R, max hold 10 κεριά."""
    wins = losses = 0
    total = 0.0
    in_trade_until = -1
    for i in signals:
        if i + 1 >= len(rows) or i + 1 <= in_trade_until:
            continue  # μία θέση τη φορά
        entry = rows[i + 1][1]
        if short:
            stop = rows[i][2]            # high του shooting star
            risk = stop - entry
        else:
            stop = rows[i][3]            # low του hammer
            risk = entry - stop
        if risk <= 0:
            continue
        target = entry - 2 * risk if short else entry + 2 * risk
        result = None
        for j in range(i + 1, min(i + 1 + MAX_HOLD, len(rows))):
            h, l = rows[j][2], rows[j][3]
            if short:
                if h >= stop:
                    result = -(risk / entry)
                    break
                if l <= target:
                    result = 2 * risk / entry
                    break
            else:
                if l <= stop:
                    result = -(risk / entry)
                    break
                if h >= target:
                    result = 2 * risk / entry
                    break
        else:
            j = min(i + MAX_HOLD, len(rows) - 1)
        if result is None:  # έξοδος λήξης χρόνου στο close
            result = (rows[j][4] / entry - 1) * (-1 if short else 1)
        result -= 2 * FEE
        total += result
        wins += result > 0
        losses += result <= 0
        in_trade_until = j
    n = wins + losses
    return {
        "trades": n,
        "win_rate": round(100 * wins / n, 1) if n else None,
        "avg_pct": round(100 * total / n, 3) if n else None,
        "total_pct": round(100 * total, 1),
    }


def analyze(name: str, rows: list[list]) -> None:
    print(f"\n=== {name} ({len(rows)} κεριά) ===")
    for kind, short in (("hammer", False), ("star", True)):
        sigs = find_signals(rows, kind)
        label = "HAMMER (long)" if kind == "hammer" else "SHOOTING STAR (short)"
        if len(sigs) < 10:
            print(f"   {label}: μόνο {len(sigs)} σήματα — ανεπαρκές δείγμα.")
            continue
        parts = []
        for hz in (1, 3, 5):
            s, b, _ = stat_edge(rows, set(sigs), hz, short)
            parts.append(f"{hz}d: {s:+.2f}% (baseline {b:+.2f}%)")
        t = simulate_trades(rows, sigs, short)
        print(f"   {label}: {len(sigs)} σήματα | " + " | ".join(parts))
        print(f"      Trade sim (SL ουρά, TP 2R): {t['trades']} trades | "
              f"win rate {t['win_rate']}% | μ.ο. {t['avg_pct']}%/trade | σύνολο {t['total_pct']}%")


def main() -> None:
    from backtest import fetch_crypto
    for sym, rows in fetch_crypto(
            ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"], "1d", "2020-09-01").items():
        analyze(sym, rows)

    from backtest_tradfi import fetch_yahoo
    print("\nΛήψη daily δεδομένων μετοχών/χρυσού (Yahoo)...")
    for sym, rows in fetch_yahoo(["SPY", "QQQ", "GC=F"], "2007-01-01").items():
        analyze(sym, rows)


if __name__ == "__main__":
    main()
