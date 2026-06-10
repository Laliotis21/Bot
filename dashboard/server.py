#!/usr/bin/env python3
"""
Dashboard server για το trading bot — διαβάζει το bot.log και σερβίρει
το UI (index.html) + ένα JSON API (/api/status). Δεν αγγίζει το bot,
δεν χρειάζεται API keys, μόνο stdlib.

ΧΡΗΣΗ
    python3 dashboard/server.py            # http://localhost:8000
    python3 dashboard/server.py --demo     # με δείγμα δεδομένων (χωρίς bot.log)
    python3 dashboard/server.py --port 9000 --log /path/to/bot.log
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(os.path.dirname(HERE), "bot.log")
MAX_TAIL_BYTES = 512 * 1024  # διαβάζουμε μόνο την ουρά του log
STALE_AFTER_SEC = 180        # 3 × default loop interval

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$")
BALANCE_RE = re.compile(r"Διαθέσιμο κεφάλαιο: ([\d.]+) (\w+)")
NEW_BALANCE_RE = re.compile(r"Νέο διαθέσιμο κεφάλαιο: ([\d.]+) (\w+)")
SYMBOL_ROW_RE = re.compile(
    r"^\s*(\S+) \| τιμή=([\d.]+) \| EMA(\d+)=([\d.]+) \| EMA(\d+)=([\d.]+) \| σήμα: (\S+)"
)
OPEN_RE = re.compile(
    r"Θέση (LONG|SHORT) ΑΝΟΙΧΤΗ: (\S+) \| είσοδος=([\d.]+) \| SL=([\d.]+).*\| TP=([\d.]+)"
)
MONITOR_RE = re.compile(
    r"Θέση (LONG|SHORT) (\S+) ανοιχτή \| mark=([\d.]+) \| είσοδος=([\d.]+) "
    r"\| SL=([\d.]+) \| TP=([\d.]+) \| μη υλοποιημένο PnL=(-?[\d.]+) (\w+)"
)
ADOPT_RE = re.compile(r"Υιοθετήθηκε υπάρχουσα θέση (LONG|SHORT) (\S+) \(είσοδος=([\d.]+)\)")
TP_HIT_RE = re.compile(r"Take Profit χτυπήθηκε στο (\S+)")
SL_HIT_RE = re.compile(r"Stop Loss χτυπήθηκε στο (\S+)")
CLOSED_RE = re.compile(r"Η θέση στο (\S+) έκλεισε")
SIGNAL_RE = re.compile(r"Σήμα (LONG|SHORT) εντοπίστηκε στο (\S+)")


def _clean_symbol(s: str) -> str:
    return s.rstrip("!.,;")


def tail_lines(path: str) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - MAX_TAIL_BYTES))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if size > MAX_TAIL_BYTES and lines:
        lines = lines[1:]  # η πρώτη γραμμή μπορεί να είναι κομμένη
    return lines


def parse_log(path: str) -> dict:
    if not os.path.exists(path):
        return {"status": "offline", "error": f"Δεν βρέθηκε το {path}"}

    lines = tail_lines(path)
    balance = None
    quote = "USDT"
    equity: list[dict] = []
    symbols: dict[str, dict] = {}
    position: dict | None = None
    last_signal: dict | None = None
    last_event: dict | None = None
    last_ts: str | None = None

    for raw in lines:
        m = TS_RE.match(raw)
        if not m:
            continue
        ts, level, msg = m.groups()
        last_ts = ts

        b = BALANCE_RE.search(msg) or NEW_BALANCE_RE.search(msg)
        if b:
            balance, quote = float(b.group(1)), b.group(2)
            equity.append({"t": ts, "v": balance})

        row = SYMBOL_ROW_RE.match(msg)
        if row:
            sym, price, f_p, f_v, s_p, s_v, sig = row.groups()
            symbols[sym] = {
                "symbol": sym, "price": float(price),
                "ema_fast_period": int(f_p), "ema_fast": float(f_v),
                "ema_slow_period": int(s_p), "ema_slow": float(s_v),
                "signal": None if sig == "κανένα" else sig, "time": ts,
            }

        sig = SIGNAL_RE.search(msg)
        if sig:
            last_signal = {"side": sig.group(1), "symbol": _clean_symbol(sig.group(2)), "time": ts}

        opened = OPEN_RE.search(msg)
        if opened:
            side, sym, entry, sl, tp = opened.groups()
            position = {
                "symbol": sym, "side": side, "entry": float(entry),
                "sl": float(sl), "tp": float(tp), "mark": None, "pnl": None,
                "since": ts,
            }
            last_event = {"type": "open", "symbol": sym, "side": side, "time": ts}

        adopted = ADOPT_RE.search(msg)
        if adopted:
            side, sym, entry = adopted.groups()
            position = {
                "symbol": sym, "side": side, "entry": float(entry),
                "sl": None, "tp": None, "mark": None, "pnl": None, "since": ts,
            }

        mon = MONITOR_RE.search(msg)
        if mon:
            side, sym, mark, entry, sl, tp, pnl, q = mon.groups()
            position = {
                "symbol": sym, "side": side, "entry": float(entry),
                "sl": float(sl), "tp": float(tp), "mark": float(mark),
                "pnl": float(pnl), "since": (position or {}).get("since", ts),
            }
            quote = q

        for rx, ev in ((TP_HIT_RE, "tp"), (SL_HIT_RE, "sl"), (CLOSED_RE, "closed")):
            hit = rx.search(msg)
            if hit:
                last_event = {"type": ev, "symbol": _clean_symbol(hit.group(1)), "time": ts}
                position = None

    status = "offline"
    if last_ts:
        age = time.time() - datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S").timestamp()
        status = "running" if age < STALE_AFTER_SEC else "stale"

    return {
        "status": status,
        "last_update": last_ts,
        "balance": balance,
        "quote": quote,
        "equity": equity[-200:],
        "symbols": sorted(symbols.values(), key=lambda s: s["symbol"]),
        "position": position,
        "last_signal": last_signal,
        "last_event": last_event,
        "logs": lines[-60:],
    }


def demo_status() -> dict:
    """Δείγμα δεδομένων για προεπισκόπηση του UI χωρίς bot.log."""
    now = datetime.now()
    rnd = random.Random(42)
    equity, bal = [], 10_000.0
    for i in range(60, 0, -1):
        bal *= 1 + rnd.uniform(-0.004, 0.006)
        equity.append({"t": (now - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                       "v": round(bal, 2)})
    mark, entry = 68420.5, 66150.0
    return {
        "status": "running",
        "last_update": now.strftime("%Y-%m-%d %H:%M:%S"),
        "balance": round(bal, 2),
        "quote": "USDT",
        "equity": equity,
        "symbols": [
            {"symbol": "BTC/USDT:USDT", "price": mark, "ema_fast_period": 9,
             "ema_fast": 67110.2, "ema_slow_period": 21, "ema_slow": 65980.7,
             "signal": None, "time": now.strftime("%Y-%m-%d %H:%M:%S")},
            {"symbol": "ETH/USDT:USDT", "price": 3551.8, "ema_fast_period": 9,
             "ema_fast": 3502.4, "ema_slow_period": 21, "ema_slow": 3489.9,
             "signal": "long", "time": now.strftime("%Y-%m-%d %H:%M:%S")},
            {"symbol": "SOL/USDT:USDT", "price": 142.31, "ema_fast_period": 9,
             "ema_fast": 147.0, "ema_slow_period": 21, "ema_slow": 149.2,
             "signal": None, "time": now.strftime("%Y-%m-%d %H:%M:%S")},
        ],
        "position": {"symbol": "BTC/USDT:USDT", "side": "LONG", "entry": entry,
                     "sl": round(entry * 0.95, 1), "tp": round(entry * 1.15, 1),
                     "mark": mark, "pnl": 325.4,
                     "since": (now - timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S")},
        "last_signal": {"side": "LONG", "symbol": "BTC/USDT:USDT",
                        "time": (now - timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S")},
        "last_event": {"type": "open", "symbol": "BTC/USDT:USDT", "side": "LONG",
                       "time": (now - timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S")},
        "logs": [f"{now.strftime('%Y-%m-%d %H:%M:%S')} [INFO] (demo) Θέση LONG BTC/USDT:USDT "
                 f"ανοιχτή | mark={mark} | είσοδος={entry} | μη υλοποιημένο PnL=325.40 USDT"],
    }


def make_handler(log_path: str, demo: bool):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=HERE, **kw)

        def do_GET(self):
            if self.path.startswith("/api/status"):
                payload = demo_status() if demo else parse_log(log_path)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/":
                self.path = "/index.html"
            super().do_GET()

        def log_message(self, fmt, *args):  # ήσυχη κονσόλα
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Trading bot dashboard server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log", default=DEFAULT_LOG, help="διαδρομή προς bot.log")
    ap.add_argument("--demo", action="store_true", help="δείγμα δεδομένων για προεπισκόπηση")
    args = ap.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), make_handler(args.log, args.demo))
    mode = "DEMO" if args.demo else f"log: {args.log}"
    print(f"Dashboard: http://localhost:{args.port}  ({mode}) — Ctrl+C για τερματισμό")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
