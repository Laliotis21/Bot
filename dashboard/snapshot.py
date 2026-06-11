#!/usr/bin/env python3
"""
Snapshot generator — παράγει docs/status.json για το GitHub Pages dashboard.

Τρέχει στο τέλος κάθε scheduled run (GitHub Actions): ρωτά το exchange για
την τρέχουσα κατάσταση (υπόλοιπο, θέσεις, σήματα, funding) και γράφει το
ίδιο payload που σερβίρει ο τοπικός dashboard server, ώστε το ίδιο UI να
δουλεύει και στατικά (Pages) και τοπικά.

Κρατά ιστορικό: docs/equity.json (καμπύλη κεφαλαίου) και συμβάντα
(σύγκριση με το προηγούμενο snapshot).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_bot import Config, ExchangeClient, TradingBot, compute_emas, detect_signal, setup_logging  # noqa: E402
from tsmom_bot import TsmomBot  # noqa: E402
from funding_bot import FundingBot  # noqa: E402

DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
ACTIONS_URL = "https://github.com/Laliotis21/Bot/actions"


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main() -> None:
    setup_logging("snapshot.log")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(DOCS, exist_ok=True)
    prev = _read_json(os.path.join(DOCS, "status.json"), {})
    events: list[dict] = prev.get("events", [])[:50]

    cfg = Config.from_env()
    client = ExchangeClient(cfg)
    ema_bot = TradingBot(client, cfg)
    tsmom = TsmomBot(client, cfg)
    funding = FundingBot(client, cfg)

    def add_event(strategy: str, kind: str, text: str) -> None:
        events.insert(0, {"time": now, "strategy": strategy, "kind": kind, "text": text})

    # --- Υπόλοιπο & equity ιστορικό ---
    balance = ema_bot.free_balance()
    eq_path = os.path.join(DOCS, "equity.json")
    equity = _read_json(eq_path, [])
    equity.append({"t": now, "v": round(balance, 2)})
    equity = equity[-1000:]
    with open(eq_path, "w", encoding="utf-8") as f:
        json.dump(equity, f, ensure_ascii=False)

    # --- EMA bot: σήματα & θέση ---
    symbols_rows = []
    position = None
    for symbol in ema_bot.symbols:
        closes, _ts = ema_bot.closed_candles(symbol)
        if not closes:
            continue
        ef, es = compute_emas(closes, cfg.ema_fast, cfg.ema_slow)
        sig = detect_signal(closes, cfg.ema_fast, cfg.ema_slow)
        symbols_rows.append({
            "symbol": symbol, "price": closes[-1],
            "ema_fast_period": cfg.ema_fast, "ema_fast": round(float(ef.iloc[-1]), 6),
            "ema_slow_period": cfg.ema_slow, "ema_slow": round(float(es.iloc[-1]), 6),
            "signal": sig, "time": now,
        })
        pos = ema_bot.get_open_position(symbol)
        if pos is not None:
            entry = float(pos.get("entryPrice") or 0)
            position = {
                "symbol": symbol, "side": (pos.get("side") or "long").upper(),
                "entry": entry, "mark": float(pos.get("markPrice") or 0) or None,
                "pnl": float(pos.get("unrealizedPnl") or 0),
                "sl": None, "tp": None, "since": prev.get("position", {}).get("since", now)
                if prev.get("position") else now,
            }
    prev_pos = prev.get("position")
    if position and not prev_pos:
        add_event("EMA", "open", f"Άνοιγμα {position['side']} {position['symbol']} @ {position['entry']:.4f}")
    elif prev_pos and not position:
        add_event("EMA", "closed", f"Κλείσιμο θέσης {prev_pos.get('symbol', '')}")

    # --- TSMOM ---
    tsmom_rows = []
    prev_tsmom = {s["symbol"]: s for s in prev.get("tsmom", {}).get("symbols", [])}
    for symbol in tsmom.symbols:
        ret, _mts = tsmom.momentum(symbol)
        if ret is None:
            continue
        pos = tsmom.open_long(symbol)
        has = pos is not None
        tsmom_rows.append({
            "symbol": symbol, "lookback": tsmom.lookback,
            "momentum": round(ret * 100, 1),
            "signal": "LONG" if ret > 0 else "FLAT",
            "position": "LONG" if has else "καμία", "time": now,
        })
        was = prev_tsmom.get(symbol, {}).get("position") == "LONG"
        if has and not was:
            add_event("TSMOM", "open", f"Άνοιγμα LONG {symbol}")
        elif was and not has:
            add_event("TSMOM", "closed", f"Κλείσιμο θέσης {symbol}")

    # --- Funding ---
    fund_rows = []
    fstate = funding.state
    prev_income = prev.get("funding", {}).get("total_income", 0.0) or 0.0
    total_income = fstate.get("paper_income", 0.0)
    for symbol in funding.symbols:
        apr = funding.trailing_apr(symbol)
        if apr is None:
            continue
        p = fstate.get("positions", {}).get(symbol)
        fund_rows.append({
            "symbol": symbol, "apr": round(apr * 100, 2), "active": p is not None,
            "notional": p.get("notional") if p else None,
            "accrued": p.get("accrued", 0.0) if p else None,
            "mode": p.get("mode", "PAPER") if p else None, "time": now,
        })
    if total_income > prev_income:
        add_event("FUND", "income",
                  f"💰 Funding εισόδημα: +{total_income - prev_income:.4f} {cfg.quote}")

    status = {
        "status": "running", "last_update": now,
        "source": "github-actions",
        "balance": round(balance, 2), "quote": cfg.quote,
        "equity": equity[-200:],
        "symbols": symbols_rows, "position": position,
        "last_signal": prev.get("last_signal"),
        "ema_status": "running",
        "tsmom": {"status": "running", "last_update": now, "symbols": tsmom_rows},
        "funding": {"status": "running", "last_update": now, "symbols": fund_rows,
                    "total_income": total_income,
                    "positions_count": len(fstate.get("positions", {}))},
        "events": events[:30],
        "logs": [f"{now} [INFO] Snapshot από GitHub Actions — αναλυτικά logs: {ACTIONS_URL}"],
    }
    with open(os.path.join(DOCS, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    print(f"Snapshot OK: balance={balance:.2f}, events={len(events)}, equity_points={len(equity)}")


if __name__ == "__main__":
    main()
