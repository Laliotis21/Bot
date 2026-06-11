#!/usr/bin/env python3
"""
Funding Harvester — delta-neutral είσπραξη funding με regime filter
===================================================================

ΣΤΡΑΤΗΓΙΚΗ (επαληθευμένη σε 3,5 χρόνια πραγματικών funding rates):
    - Θέση: LONG spot + SHORT perp ίσου μεγέθους -> μηδενική έκθεση στην τιμή.
      Το short perp ΕΙΣΠΡΑΤΤΕΙ το funding όταν είναι θετικό (κάθε 8 ώρες).
    - Regime filter: είσοδος όταν το trailing 30ήμερο funding (ετησιοποιημένο)
      > FUNDING_ENTER_APR (default 5%), έξοδος όταν < FUNDING_EXIT_APR (default 0%).
      Ιστορικά: ~8% APR εντός, 1 αρνητικός μήνας στους 35.

ΛΕΙΤΟΥΡΓΙΕΣ
    - PAPER (default, FUNDING_EXECUTE=false): πραγματικά rates & αποφάσεις,
      λογιστική παρακολούθηση εισοδήματος (ακριβής — το funding είναι
      ντετερμινιστικό: rate x notional, χωρίς εξάρτηση από εκτέλεση).
    - EXECUTE (FUNDING_EXECUTE=true): πραγματικές εντολές — αγορά spot +
      short perp. ΠΡΟΣΟΧΗ: θέλει λογαριασμό χωρίς αντικρουόμενες θέσεις στα
      ίδια σύμβολα (π.χ. όχι μαζί με TSMOM longs σε one-way mode).

ΧΡΗΣΗ
    python3 funding_bot.py            # κανονικός βρόχος (paper εκτός αν .env λέει αλλιώς)
    python3 funding_bot.py --check    # read-only: τρέχοντα APRs & αποφάσεις
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import ccxt

from trading_bot import Config, ExchangeClient, log, setup_logging

STATE_FILE = "funding_state.json"
DAY_MS = 86_400_000


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


class FundingBot:
    def __init__(self, client: ExchangeClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        raw = [s.strip() for s in _env("FUNDING_SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]
        self.symbols = [client.resolve_symbol(s) for s in raw]
        self.alloc_pct = float(_env("FUNDING_ALLOCATION_PCT", "0.3"))
        self.enter_apr = float(_env("FUNDING_ENTER_APR", "0.05"))
        self.exit_apr = float(_env("FUNDING_EXIT_APR", "0.0"))
        self.interval = int(_env("FUNDING_CHECK_INTERVAL", "3600"))
        self.execute = _env("FUNDING_EXECUTE", "false").lower() in ("1", "true", "yes", "on")
        self.spot: Optional[ccxt.binance] = None  # δημιουργείται μόνο σε execute mode
        self.state = self._load_state()

    # ---- State --------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"positions": {}, "paper_income": 0.0}

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except OSError as exc:
            log.warning("Αδυναμία αποθήκευσης κατάστασης: %s", exc)

    # ---- Δεδομένα ------------------------------------------------------------
    def trailing_apr(self, symbol: str, days: int = 30) -> Optional[float]:
        """Ετησιοποιημένο funding του τελευταίου 30ημέρου (το short το εισπράττει)."""
        since = int(time.time() * 1000) - days * DAY_MS
        hist = self.client.safe_call(
            self.client.exchange.fetch_funding_rate_history, symbol, since, 1000
        )
        if not hist:
            return None
        total = sum(float(h["fundingRate"]) for h in hist)
        span_days = max((hist[-1]["timestamp"] - hist[0]["timestamp"]) / DAY_MS, 1)
        return total * (365 / span_days)

    def new_funding_income(self, symbol: str, notional: float, since_ts: int) -> tuple[float, int]:
        """Εισόδημα από funding events μετά το since_ts (paper accounting)."""
        hist = self.client.safe_call(
            self.client.exchange.fetch_funding_rate_history, symbol, since_ts + 1, 1000
        )
        if not hist:
            return 0.0, since_ts
        income = sum(float(h["fundingRate"]) for h in hist) * notional
        return income, int(hist[-1]["timestamp"])

    def total_balance(self) -> float:
        bal = self.client.safe_call(self.client.exchange.fetch_balance)
        q = bal.get(self.cfg.quote, {})
        return float(q.get("total") or q.get("free") or 0.0)

    # ---- Εκτέλεση (EXECUTE mode) ----------------------------------------------
    def _spot_client(self) -> ccxt.binance:
        if self.spot is None:
            self.spot = ccxt.binance({
                "apiKey": self.cfg.api_key, "secret": self.cfg.api_secret,
                "enableRateLimit": True,
            })
            if self.cfg.use_testnet and hasattr(self.spot, "enable_demo_trading"):
                self.spot.enable_demo_trading(True)
            self.spot.load_markets()
        return self.spot

    def _open_pair(self, symbol: str, notional: float) -> Optional[dict]:
        """Αγορά spot + short perp ίσου μεγέθους. Επιστρέφει τα στοιχεία θέσης."""
        ex = self.client.exchange
        spot = self._spot_client()
        spot_symbol = symbol.split(":")[0]            # BTC/USDT:USDT -> BTC/USDT
        price = float(self.client.safe_call(ex.fetch_ticker, symbol).get("last") or 0)
        if price <= 0:
            log.error("Μη διαθέσιμη τιμή για %s.", symbol)
            return None
        amount = float(ex.amount_to_precision(symbol, notional / price))
        if amount <= 0:
            log.error("Πολύ μικρό μέγεθος για %s.", symbol)
            return None
        try:
            spot.create_order(spot_symbol, "market", "buy", amount)
        except ccxt.ExchangeError as exc:
            log.error("Αποτυχία αγοράς spot %s: %s", spot_symbol, exc)
            return None
        try:
            self.client.safe_call(ex.set_leverage, 1, symbol)
            self.client.place_order(symbol, "market", "sell", amount)
        except ccxt.ExchangeError as exc:
            log.error("Αποτυχία short perp %s: %s — ΞΕΚΑΝΕ το spot σκέλος χειροκίνητα!", symbol, exc)
            return None
        return {"amount": amount, "entry_price": price}

    def _close_pair(self, symbol: str, amount: float) -> bool:
        ex = self.client.exchange
        spot = self._spot_client()
        spot_symbol = symbol.split(":")[0]
        ok = True
        try:
            self.client.place_order(symbol, "market", "buy", amount, None, {"reduceOnly": True})
        except ccxt.ExchangeError as exc:
            log.error("Αποτυχία κλεισίματος short %s: %s", symbol, exc)
            ok = False
        try:
            spot.create_order(spot_symbol, "market", "sell", amount)
        except ccxt.ExchangeError as exc:
            log.error("Αποτυχία πώλησης spot %s: %s", spot_symbol, exc)
            ok = False
        return ok

    # ---- Κύρια λογική -----------------------------------------------------------
    def step(self, dry_run: bool = False) -> None:
        balance = self.total_balance()
        per_symbol = balance * self.alloc_pct / max(len(self.symbols), 1)
        mode = "EXECUTE" if self.execute else "PAPER"
        positions = self.state.setdefault("positions", {})

        for symbol in self.symbols:
            apr = self.trailing_apr(symbol)
            if apr is None:
                log.warning("%s: δεν βρέθηκαν funding δεδομένα.", symbol)
                continue
            p = positions.get(symbol)
            active = p is not None
            log.info("%s | trailing 30d funding: %+.2f%% APR | θέση: %s | όριο εισόδου: %.1f%%",
                     symbol, apr * 100, "ΕΝΕΡΓΗ" if active else "εκτός", self.enter_apr * 100)

            # Paper accounting εισοδήματος για ενεργές θέσεις
            if active and not dry_run:
                income, last_ts = self.new_funding_income(symbol, p["notional"], p["last_funding_ts"])
                if income != 0.0:
                    p["accrued"] = round(p.get("accrued", 0.0) + income, 4)
                    p["last_funding_ts"] = last_ts
                    self.state["paper_income"] = round(self.state.get("paper_income", 0.0) + income, 4)
                    log.info("   💰 Funding εισόδημα %s: %+.4f %s (σύνολο θέσης: %.4f)",
                             symbol, income, self.cfg.quote, p["accrued"])

            if not active and apr > self.enter_apr:
                if dry_run:
                    log.info("   [check] Θα άνοιγε delta-neutral θέση %.2f %s (%s).",
                             per_symbol, self.cfg.quote, mode)
                    continue
                log.info("➡️  Είσοδος funding harvest %s: notional %.2f %s [%s]",
                         symbol, per_symbol, self.cfg.quote, mode)
                opened = self._open_pair(symbol, per_symbol) if self.execute else {"amount": None}
                if opened is not None:
                    positions[symbol] = {
                        "notional": per_symbol, "amount": opened.get("amount"),
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "last_funding_ts": int(time.time() * 1000),
                        "accrued": 0.0, "mode": mode,
                    }
                    self._save_state()
            elif active and apr < self.exit_apr:
                if dry_run:
                    log.info("   [check] Θα έκλεινε τη θέση (APR κάτω από όριο εξόδου).")
                    continue
                log.info("⬅️  Έξοδος funding harvest %s (APR %+.2f%% < %.1f%%). Εισπράχθηκαν: %.4f %s",
                         symbol, apr * 100, self.exit_apr * 100,
                         p.get("accrued", 0.0), self.cfg.quote)
                if not self.execute or self._close_pair(symbol, p.get("amount") or 0):
                    del positions[symbol]
                    self._save_state()

        log.info("Σύνολο paper funding εισοδήματος μέχρι σήμερα: %.4f %s",
                 self.state.get("paper_income", 0.0), self.cfg.quote)

    def run(self) -> None:
        mode = "EXECUTE" if self.execute else "PAPER"
        log.info("=== Εκκίνηση Funding Harvester [%s] ===", mode)
        log.info("Assets: %s | Allocation: %.0f%% | Είσοδος >%.1f%% APR / Έξοδος <%.1f%% | Έλεγχος κάθε %dmin",
                 ", ".join(self.symbols), self.alloc_pct * 100,
                 self.enter_apr * 100, self.exit_apr * 100, self.interval // 60)
        while True:
            try:
                self.step()
            except ccxt.AuthenticationError as exc:
                log.error("Σφάλμα αυθεντικοποίησης: %s", exc)
                break
            except Exception as exc:  # noqa: BLE001
                log.exception("Μη αναμενόμενο σφάλμα: %s", exc)
            time.sleep(self.interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Funding harvester (delta-neutral, regime filter).")
    ap.add_argument("--check", action="store_true", help="read-only έλεγχος APRs & αποφάσεων")
    ap.add_argument("--once", action="store_true", help="ένα πέρασμα και έξοδος (για cron)")
    args = ap.parse_args()

    setup_logging("funding.log")
    cfg = Config.from_env()
    if not cfg.api_key or not cfg.api_secret:
        log.error("Λείπουν API_KEY/API_SECRET στο .env.")
        sys.exit(1)
    bot = FundingBot(ExchangeClient(cfg), cfg)

    if args.check:
        bot.step(dry_run=True)
    elif args.once:
        bot.step()
    else:
        bot.run()


if __name__ == "__main__":
    main()
