#!/usr/bin/env python3
"""
Ανάλυση ιστορικών funding rates (Binance USDⓂ perps) για αξιολόγηση
στρατηγικής delta-neutral funding harvest (long spot + short perp).

Σε αυτή τη θέση: εισπράττεις το funding όταν είναι θετικό, το πληρώνεις
όταν είναι αρνητικό. Μηδενική έκθεση στην κατεύθυνση της τιμής.

ΧΡΗΣΗ
    python3 funding_analysis.py                    # από 2023-01-01
    python3 funding_analysis.py --since 2021-01-01
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import ccxt

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]


def fetch_funding_history(ex: ccxt.Exchange, symbol: str, since_ms: int) -> list[dict]:
    out: list[dict] = []
    cursor = since_ms
    while True:
        batch = ex.fetch_funding_rate_history(symbol, since=cursor, limit=1000)
        if not batch:
            break
        out.extend(b for b in batch if not out or b["timestamp"] > out[-1]["timestamp"])
        if len(batch) < 1000:
            break
        cursor = out[-1]["timestamp"] + 1
        time.sleep(ex.rateLimit / 1000)
    return out


def analyze(symbol: str, rates: list[dict]) -> dict:
    by_month: dict[str, float] = defaultdict(float)
    by_year: dict[str, float] = defaultdict(float)
    positive = 0
    for r in rates:
        ts = datetime.fromtimestamp(r["timestamp"] / 1000, tz=timezone.utc)
        rate = float(r["fundingRate"])
        by_month[ts.strftime("%Y-%m")] += rate
        by_year[ts.strftime("%Y")] += rate
        if rate > 0:
            positive += 1

    months = sorted(by_month)
    monthly = [by_month[m] for m in months]
    neg_months = [m for m in months if by_month[m] < 0]
    total = sum(monthly)
    years_span = len(rates) / (3 * 365.25)  # 3 funding periods/ημέρα

    return {
        "symbol": symbol,
        "from": months[0], "to": months[-1],
        "periods": len(rates),
        "pct_periods_positive": round(100 * positive / len(rates), 1),
        "apr_avg_pct": round(100 * total / years_span, 2),
        "by_year_pct": {y: round(100 * v, 2) for y, v in sorted(by_year.items())},
        "monthly_avg_pct": round(100 * total / len(months), 3),
        "best_month": max(months, key=lambda m: by_month[m]),
        "best_month_pct": round(100 * max(monthly), 2),
        "worst_month": min(months, key=lambda m: by_month[m]),
        "worst_month_pct": round(100 * min(monthly), 2),
        "negative_months": len(neg_months),
        "total_months": len(months),
        "by_month_pct": {m: round(100 * by_month[m], 3) for m in months},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Ανάλυση funding rates για delta-neutral harvest.")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--out", default="funding_analysis.json")
    args = ap.parse_args()

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since_ms = int(datetime.fromisoformat(args.since)
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)

    results = []
    for s in SYMBOLS:
        print(f"Λήψη funding history {s} από {args.since}...")
        rates = fetch_funding_history(ex, s, since_ms)
        r = analyze(s, rates)
        results.append(r)
        print(f"\n=== {s} ({r['from']} -> {r['to']}, {r['periods']} περίοδοι 8h) ===")
        print(f"   Μέσο APR              : {r['apr_avg_pct']}%")
        print(f"   Ανά έτος              : {r['by_year_pct']}")
        print(f"   Μέσος μήνας           : {r['monthly_avg_pct']}%")
        print(f"   Περίοδοι με θετικό    : {r['pct_periods_positive']}%")
        print(f"   Αρνητικοί μήνες       : {r['negative_months']}/{r['total_months']}")
        print(f"   Καλύτερος μήνας       : {r['best_month']} ({r['best_month_pct']}%)")
        print(f"   Χειρότερος μήνας      : {r['worst_month']} ({r['worst_month_pct']}%)")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nΑναλυτικά (ανά μήνα): {args.out}")


if __name__ == "__main__":
    main()
