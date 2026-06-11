#!/usr/bin/env python3
"""
Dashboard server — ενιαία εικόνα και των τριών strategies:
  - EMA bot        (bot.log)
  - TSMOM bot      (tsmom.log + tsmom_state.json)
  - Funding bot    (funding.log + funding_state.json)

Σερβίρει το UI (index.html) + JSON API (/api/status). Δεν αγγίζει τα bots,
δεν χρειάζεται API keys, μόνο stdlib.

ΧΡΗΣΗ
    python3 dashboard/server.py            # http://localhost:8000
    python3 dashboard/server.py --demo     # με δείγμα δεδομένων
    python3 dashboard/server.py --port 9000 --dir /path/to/bot/dir
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
DEFAULT_DIR = os.path.dirname(HERE)
MAX_TAIL_BYTES = 512 * 1024
STALE_AFTER_SEC = 180          # EMA bot: loop 60s
STALE_TSMOM_SEC = 3 * 21600    # TSMOM: loop 6h
STALE_FUND_SEC = 3 * 3600      # Funding: loop 1h

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$")

# --- EMA bot (bot.log) ---
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

# --- TSMOM bot (tsmom.log) ---
TSMOM_ROW_RE = re.compile(
    r"^(\S+) \| (\d+)μηνο momentum: ([+-][\d.]+)% \| σήμα: (\S+) \| θέση: (\S+)"
)
TSMOM_OPEN_RE = re.compile(r"TSMOM LONG (\S+): ([\d.]+) \(~([\d.]+)")
TSMOM_CLOSE_RE = re.compile(r"TSMOM ΚΛΕΙΣΙΜΟ θέσης (\S+)")

# --- Funding bot (funding.log) ---
FUND_ROW_RE = re.compile(
    r"^(\S+) \| trailing 30d funding: ([+-][\d.]+)% APR \| θέση: (\S+)"
)
FUND_INCOME_RE = re.compile(r"Funding εισόδημα (\S+): ([+-][\d.]+) (\w+)")
FUND_ENTER_RE = re.compile(r"Είσοδος funding harvest (\S+): notional ([\d.]+)")
FUND_EXIT_RE = re.compile(r"Έξοδος funding harvest (\S+)")


def _clean_symbol(s: str) -> str:
    return s.rstrip("!.,;")


def tail_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - MAX_TAIL_BYTES))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if size > MAX_TAIL_BYTES and lines:
        lines = lines[1:]
    return lines


def _status_from_ts(last_ts: str | None, stale_sec: int) -> str:
    if not last_ts:
        return "offline"
    age = time.time() - datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S").timestamp()
    return "running" if age < stale_sec else "stale"


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def parse_ema(path: str, events: list) -> dict:
    lines = tail_lines(path)
    balance = None
    quote = "USDT"
    equity: list[dict] = []
    symbols: dict[str, dict] = {}
    position: dict | None = None
    last_signal: dict | None = None
    last_ts: str | None = None

    for raw in lines:
        m = TS_RE.match(raw)
        if not m:
            continue
        ts, _level, msg = m.groups()
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
            events.append({"time": ts, "strategy": "EMA", "kind": "signal",
                           "text": f"Σήμα {sig.group(1)} στο {_clean_symbol(sig.group(2))}"})

        opened = OPEN_RE.search(msg)
        if opened:
            side, sym, entry, sl, tp = opened.groups()
            position = {"symbol": sym, "side": side, "entry": float(entry),
                        "sl": float(sl), "tp": float(tp), "mark": None, "pnl": None, "since": ts}
            events.append({"time": ts, "strategy": "EMA", "kind": "open",
                           "text": f"Άνοιγμα {side} {sym} @ {entry}"})

        adopted = ADOPT_RE.search(msg)
        if adopted:
            side, sym, entry = adopted.groups()
            position = {"symbol": sym, "side": side, "entry": float(entry),
                        "sl": None, "tp": None, "mark": None, "pnl": None, "since": ts}

        mon = MONITOR_RE.search(msg)
        if mon:
            side, sym, mark, entry, sl, tp, pnl, q = mon.groups()
            position = {"symbol": sym, "side": side, "entry": float(entry),
                        "sl": float(sl), "tp": float(tp), "mark": float(mark),
                        "pnl": float(pnl), "since": (position or {}).get("since", ts)}
            quote = q

        for rx, kind, label in ((TP_HIT_RE, "tp", "🎯 Take Profit"),
                                (SL_HIT_RE, "sl", "🛑 Stop Loss"),
                                (CLOSED_RE, "closed", "Κλείσιμο θέσης")):
            hit = rx.search(msg)
            if hit:
                sym = _clean_symbol(hit.group(1))
                events.append({"time": ts, "strategy": "EMA", "kind": kind,
                               "text": f"{label} — {sym}"})
                position = None
                break

    return {
        "status": _status_from_ts(last_ts, STALE_AFTER_SEC),
        "last_update": last_ts, "balance": balance, "quote": quote,
        "equity": equity[-200:], "symbols": sorted(symbols.values(), key=lambda s: s["symbol"]),
        "position": position, "last_signal": last_signal,
    }


def parse_tsmom(path: str, events: list) -> dict:
    lines = tail_lines(path)
    symbols: dict[str, dict] = {}
    last_ts = None
    for raw in lines:
        m = TS_RE.match(raw)
        if not m:
            continue
        ts, _lvl, msg = m.groups()
        last_ts = ts
        row = TSMOM_ROW_RE.match(msg)
        if row:
            sym, lb, mom, sig, pos = row.groups()
            symbols[sym] = {"symbol": sym, "lookback": int(lb), "momentum": float(mom),
                            "signal": sig, "position": pos, "time": ts}
        o = TSMOM_OPEN_RE.search(msg)
        if o:
            events.append({"time": ts, "strategy": "TSMOM", "kind": "open",
                           "text": f"Άνοιγμα LONG {_clean_symbol(o.group(1))} (~{o.group(3)} USDT)"})
        c = TSMOM_CLOSE_RE.search(msg)
        if c:
            events.append({"time": ts, "strategy": "TSMOM", "kind": "closed",
                           "text": f"Κλείσιμο θέσης {_clean_symbol(c.group(1))}"})
    return {
        "status": _status_from_ts(last_ts, STALE_TSMOM_SEC),
        "last_update": last_ts,
        "symbols": sorted(symbols.values(), key=lambda s: s["symbol"]),
    }


def parse_funding(path: str, state_path: str, events: list) -> dict:
    lines = tail_lines(path)
    symbols: dict[str, dict] = {}
    last_ts = None
    for raw in lines:
        m = TS_RE.match(raw)
        if not m:
            continue
        ts, _lvl, msg = m.groups()
        last_ts = ts
        row = FUND_ROW_RE.match(msg)
        if row:
            sym, apr, pos = row.groups()
            symbols[sym] = {"symbol": sym, "apr": float(apr),
                            "active": pos == "ΕΝΕΡΓΗ", "time": ts}
        inc = FUND_INCOME_RE.search(msg)
        if inc:
            events.append({"time": ts, "strategy": "FUND", "kind": "income",
                           "text": f"💰 Funding {_clean_symbol(inc.group(1))}: {inc.group(2)} {inc.group(3)}"})
        en = FUND_ENTER_RE.search(msg)
        if en:
            events.append({"time": ts, "strategy": "FUND", "kind": "open",
                           "text": f"Είσοδος delta-neutral {_clean_symbol(en.group(1))} ({en.group(2)} USDT)"})
        ex = FUND_EXIT_RE.search(msg)
        if ex:
            events.append({"time": ts, "strategy": "FUND", "kind": "closed",
                           "text": f"Έξοδος delta-neutral {_clean_symbol(ex.group(1))}"})

    state = _read_json(state_path)
    positions = state.get("positions", {})
    for sym, p in positions.items():
        if sym in symbols:
            symbols[sym]["active"] = True
            symbols[sym]["notional"] = p.get("notional")
            symbols[sym]["accrued"] = p.get("accrued", 0.0)
            symbols[sym]["mode"] = p.get("mode", "PAPER")
    return {
        "status": _status_from_ts(last_ts, STALE_FUND_SEC),
        "last_update": last_ts,
        "symbols": sorted(symbols.values(), key=lambda s: s["symbol"]),
        "total_income": state.get("paper_income", 0.0),
        "positions_count": len(positions),
    }


def merged_logs(base: str) -> list[str]:
    out = []
    for fname, tag in (("bot.log", "EMA  "), ("tsmom.log", "TSMOM"), ("funding.log", "FUND ")):
        for line in tail_lines(os.path.join(base, fname))[-80:]:
            if TS_RE.match(line):
                out.append(f"{line[:19]} [{tag}]{line[19:]}")
    out.sort()
    return out[-80:]


def build_status(base: str) -> dict:
    events: list[dict] = []
    ema = parse_ema(os.path.join(base, "bot.log"), events)
    tsmom = parse_tsmom(os.path.join(base, "tsmom.log"), events)
    funding = parse_funding(os.path.join(base, "funding.log"),
                            os.path.join(base, "funding_state.json"), events)
    events.sort(key=lambda e: e["time"], reverse=True)

    overall = "offline"
    if any(x["status"] == "running" for x in (ema, tsmom, funding)):
        overall = "running"
    elif any(x["status"] == "stale" for x in (ema, tsmom, funding)):
        overall = "stale"

    return {
        "status": overall,
        "last_update": max(filter(None, [ema["last_update"], tsmom["last_update"],
                                         funding["last_update"]]), default=None),
        "balance": ema["balance"], "quote": ema["quote"],
        "equity": ema["equity"], "symbols": ema["symbols"],
        "position": ema["position"], "last_signal": ema["last_signal"],
        "ema_status": ema["status"],
        "tsmom": tsmom,
        "funding": funding,
        "events": events[:30],
        "logs": merged_logs(base),
    }


def demo_status() -> dict:
    now = datetime.now()
    rnd = random.Random(42)
    equity, bal = [], 10_000.0
    for i in range(60, 0, -1):
        bal *= 1 + rnd.uniform(-0.004, 0.006)
        equity.append({"t": (now - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                       "v": round(bal, 2)})
    t = now.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "status": "running", "last_update": t, "balance": round(bal, 2), "quote": "USDT",
        "equity": equity, "ema_status": "running",
        "symbols": [{"symbol": "SOL/USDT:USDT", "price": 142.31, "ema_fast_period": 9,
                     "ema_fast": 147.0, "ema_slow_period": 21, "ema_slow": 149.2,
                     "signal": None, "time": t}],
        "position": {"symbol": "SOL/USDT:USDT", "side": "LONG", "entry": 138.0,
                     "sl": 131.1, "tp": 158.7, "mark": 142.31, "pnl": 31.2,
                     "since": (now - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")},
        "last_signal": {"side": "LONG", "symbol": "SOL/USDT:USDT",
                        "time": (now - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")},
        "tsmom": {"status": "running", "last_update": t, "symbols": [
            {"symbol": "BTC/USDT:USDT", "lookback": 12, "momentum": 18.4,
             "signal": "LONG", "position": "LONG", "time": t},
            {"symbol": "ETH/USDT:USDT", "lookback": 12, "momentum": -7.2,
             "signal": "FLAT", "position": "καμία", "time": t}]},
        "funding": {"status": "running", "last_update": t, "total_income": 14.62,
                    "positions_count": 1, "symbols": [
            {"symbol": "BTC/USDT:USDT", "apr": 12.5, "active": True,
             "notional": 750.0, "accrued": 9.31, "mode": "PAPER", "time": t},
            {"symbol": "ETH/USDT:USDT", "apr": 3.1, "active": False, "time": t}]},
        "events": [
            {"time": t, "strategy": "FUND", "kind": "income", "text": "💰 Funding BTC/USDT:USDT: +0.0721 USDT"},
            {"time": (now - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
             "strategy": "TSMOM", "kind": "open", "text": "Άνοιγμα LONG BTC/USDT:USDT (~1250 USDT)"},
            {"time": (now - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"),
             "strategy": "EMA", "kind": "open", "text": "Άνοιγμα LONG SOL/USDT:USDT @ 138.0"},
        ],
        "logs": [f"{t} [EMA  ] [INFO] (demo) Θέση LONG SOL/USDT:USDT ανοιχτή | PnL=+31.20 USDT"],
    }


def make_handler(base_dir: str, demo: bool):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=HERE, **kw)

        def do_GET(self):
            if self.path.startswith("/api/status"):
                payload = demo_status() if demo else build_status(base_dir)
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

        def log_message(self, fmt, *args):
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Trading bots dashboard server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="φάκελος με logs/state των bots")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), make_handler(args.dir, args.demo))
    mode = "DEMO" if args.demo else f"dir: {args.dir}"
    print(f"Dashboard: http://localhost:{args.port}  ({mode}) — Ctrl+C για τερματισμό")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
