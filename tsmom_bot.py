#!/usr/bin/env python3
"""
TSMOM Bot — 12μηνο Time-Series Momentum, long-or-flat (BTC + ETH)
=================================================================

ΣΤΡΑΤΗΓΙΚΗ (Moskowitz/Liu-Tsyvinski, επαληθευμένη στο backtest μας):
    - Για κάθε asset: απόδοση τελευταίων N κλεισμένων μηνών (default 12).
    - Θετική  -> LONG με (allocation / πλήθος assets) του συνολικού κεφαλαίου.
    - Αρνητική -> FLAT (κλείσιμο θέσης, αναμονή).
    - Έλεγχος κάθε TSMOM_CHECK_INTERVAL (default 6h) — ενεργεί ΜΟΝΟ όταν
      αλλάζει το σήμα σε νέο κλεισμένο μηνιαίο κερί (≈2-3 αλλαγές/χρόνο).
    - 1x ISOLATED, χωρίς SL/TP: η έξοδος της στρατηγικής ΕΙΝΑΙ το μηνιαίο σήμα.

Ιστορικό backtest (2018-2026, με fees): CAGR ~30%, maxDD ~-43%, Sharpe 0.81.

ΠΡΟΣΟΧΗ: Τρέχει στα ΔΙΚΑ του σύμβολα (TSMOM_SYMBOLS). Μην τα μοιράζεσαι με
το EMA bot στον ίδιο λογαριασμό — θα συγκρουστούν οι θέσεις.

ΧΡΗΣΗ
    python3 tsmom_bot.py            # κανονικός βρόχος
    python3 tsmom_bot.py --check    # read-only: σήματα & θέσεις, χωρίς orders
    python3 tsmom_bot.py --once     # ένα rebalance και έξοδος
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import ccxt

from trading_bot import Config, ExchangeClient, log, setup_logging

STATE_FILE = "tsmom_state.json"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


class TsmomBot:
    def __init__(self, client: ExchangeClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        raw = [s.strip() for s in _env("TSMOM_SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]
        self.symbols = [client.resolve_symbol(s) for s in raw]
        self.lookback = int(_env("TSMOM_LOOKBACK_MONTHS", "12"))
        self.alloc_pct = float(_env("TSMOM_ALLOCATION_PCT", "0.5"))
        self.interval = int(_env("TSMOM_CHECK_INTERVAL", "21600"))
        self.state: dict[str, int] = self._load_state()  # symbol -> month_ts που ενεργήσαμε

    # ---- State ------------------------------------------------------------
    def _load_state(self) -> dict[str, int]:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return {k: int(v) for k, v in json.load(f).get("acted_month", {}).items()}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"acted_month": self.state}, f)
        except OSError as exc:
            log.warning("Αδυναμία αποθήκευσης κατάστασης: %s", exc)

    # ---- Δεδομένα ----------------------------------------------------------
    def momentum(self, symbol: str) -> tuple[Optional[float], Optional[int]]:
        """Απόδοση `lookback` κλεισμένων μηνών & timestamp του τελευταίου κλεισμένου μήνα."""
        ohlcv = self.client.safe_call(
            self.client.exchange.fetch_ohlcv, symbol, "1M", None, self.lookback + 3
        )
        if not ohlcv or len(ohlcv) < self.lookback + 2:
            return None, None
        closed = ohlcv[:-1]  # το τελευταίο μηνιαίο κερί είναι ανοιχτό
        if len(closed) < self.lookback + 1:
            return None, None
        ret = closed[-1][4] / closed[-1 - self.lookback][4] - 1
        return ret, int(closed[-1][0])

    def total_balance(self) -> float:
        bal = self.client.safe_call(self.client.exchange.fetch_balance)
        q = bal.get(self.cfg.quote, {})
        return float(q.get("total") or q.get("free") or 0.0)

    def open_long(self, symbol: str) -> Optional[dict]:
        positions = self.client.safe_call(self.client.exchange.fetch_positions, [symbol])
        for p in positions:
            if abs(float(p.get("contracts") or 0)) > 0:
                return p
        return None

    # ---- Εκτέλεση -----------------------------------------------------------
    def _enter_long(self, symbol: str, notional: float) -> bool:
        ex = self.client.exchange
        try:
            self.client.safe_call(ex.set_margin_mode, "isolated", symbol)
        except ccxt.ExchangeError:
            pass  # ήδη isolated ή μη κρίσιμο — η θέση είναι 1x έτσι κι αλλιώς
        try:
            self.client.safe_call(ex.set_leverage, 1, symbol)
        except ccxt.ExchangeError as exc:
            log.error("Αδυναμία ορισμού leverage 1x για %s: %s — παράλειψη εισόδου.", symbol, exc)
            return False
        ticker = self.client.safe_call(ex.fetch_ticker, symbol)
        price = float(ticker.get("last") or 0)
        if price <= 0:
            log.error("Μη διαθέσιμη τιμή για %s.", symbol)
            return False
        amount = float(ex.amount_to_precision(symbol, notional / price))
        if amount <= 0:
            log.error("Πολύ μικρό μέγεθος θέσης για %s (notional %.2f).", symbol, notional)
            return False
        try:
            self.client.place_order(symbol, "market", "buy", amount)
        except ccxt.ExchangeError as exc:
            log.error("Αποτυχία ανοίγματος LONG %s: %s", symbol, exc)
            return False
        log.info("✅ TSMOM LONG %s: %.6f (~%.2f %s @ ~%.4f)",
                 symbol, amount, notional, self.cfg.quote, price)
        return True

    def _close_position(self, symbol: str, pos: dict) -> bool:
        amount = abs(float(pos.get("contracts") or 0))
        side = "sell" if (pos.get("side") or "long") == "long" else "buy"
        try:
            self.client.place_order(symbol, "market", side, amount, None, {"reduceOnly": True})
        except ccxt.ExchangeError as exc:
            log.error("Αποτυχία κλεισίματος θέσης %s: %s", symbol, exc)
            return False
        log.info("🔁 TSMOM ΚΛΕΙΣΙΜΟ θέσης %s (%.6f).", symbol, amount)
        return True

    # ---- Rebalance -----------------------------------------------------------
    def rebalance(self, dry_run: bool = False) -> None:
        balance = self.total_balance()
        per_symbol = balance * self.alloc_pct / max(len(self.symbols), 1)
        log.info("Κεφάλαιο: %.2f %s | TSMOM allocation: %.0f%% -> %.2f %s ανά asset.",
                 balance, self.cfg.quote, self.alloc_pct * 100, per_symbol, self.cfg.quote)

        for symbol in self.symbols:
            ret, month_ts = self.momentum(symbol)
            if ret is None:
                log.warning("%s: ανεπαρκή μηνιαία δεδομένα — παράλειψη.", symbol)
                continue
            pos = self.open_long(symbol)
            have = pos is not None and (pos.get("side") or "long") == "long"
            want = ret > 0
            log.info("%s | %dμηνο momentum: %+.1f%% | σήμα: %s | θέση: %s",
                     symbol, self.lookback, ret * 100,
                     "LONG" if want else "FLAT", "LONG" if have else "καμία")

            if want == have:
                self.state[symbol] = month_ts  # σύμφωνη θέση — σημείωσε τον μήνα
                continue
            if self.state.get(symbol) == month_ts and not dry_run:
                # Έχουμε ήδη ενεργήσει σε αυτό το μηνιαίο κερί και η θέση
                # άλλαξε εξωτερικά; Μην το ξανα-ανοίξεις σιωπηλά — μόνο log.
                log.warning("%s: ασυμφωνία θέσης/σήματος στον ίδιο μήνα — έλεγξε χειροκίνητα.", symbol)
                continue
            if dry_run:
                log.info("   [check] Θα %s.", "άνοιγε LONG" if want else "έκλεινε τη θέση")
                continue
            ok = self._enter_long(symbol, per_symbol) if want else self._close_position(symbol, pos)
            if ok:
                self.state[symbol] = month_ts
                self._save_state()

    def run(self) -> None:
        log.info("=== Εκκίνηση TSMOM Bot (%dμηνο momentum, long-or-flat) ===", self.lookback)
        log.info("Assets: %s | Allocation: %.0f%% | Έλεγχος κάθε %dh",
                 ", ".join(self.symbols), self.alloc_pct * 100, self.interval // 3600)
        while True:
            try:
                self.rebalance()
            except ccxt.AuthenticationError as exc:
                log.error("Σφάλμα αυθεντικοποίησης: %s", exc)
                break
            except Exception as exc:  # noqa: BLE001 — ο βρόχος δεν πέφτει
                log.exception("Μη αναμενόμενο σφάλμα: %s", exc)
            time.sleep(self.interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="TSMOM bot (12μηνο momentum, long-or-flat).")
    ap.add_argument("--check", action="store_true", help="read-only έλεγχος σημάτων")
    ap.add_argument("--once", action="store_true", help="ένα rebalance και έξοδος")
    args = ap.parse_args()

    setup_logging("tsmom.log")
    cfg = Config.from_env()
    if not cfg.api_key or not cfg.api_secret:
        log.error("Λείπουν API_KEY/API_SECRET στο .env.")
        sys.exit(1)
    bot = TsmomBot(ExchangeClient(cfg), cfg)

    if args.check:
        bot.rebalance(dry_run=True)
    elif args.once:
        bot.rebalance()
    else:
        bot.run()


if __name__ == "__main__":
    main()
