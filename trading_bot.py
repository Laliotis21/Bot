#!/usr/bin/env python3
"""
Trading Bot — EMA 9/21 Crossover σε Crypto Futures (Binance USDⓂ, CCXT)
=======================================================================

ΣΤΡΑΤΗΓΙΚΗ
    - Σάρωση BTC/USDT, ETH/USDT, SOL/USDT στο Daily (1d) timeframe.
    - Σήμα LONG  : ο EMA9 διασταυρώνεται ΠΑΝΩ από τον EMA21 (στο κλείσιμο ημέρας).
    - Σήμα SHORT : ο EMA9 διασταυρώνεται ΚΑΤΩ από τον EMA21 (στο κλείσιμο ημέρας).
    - First-Come-First-Served: μία θέση τη φορά. Όσο υπάρχει ανοιχτή θέση,
      αγνοούμε νέα σήματα μέχρι να ελευθερωθεί κεφάλαιο.
    - Compounding: κάθε νέο trade χρησιμοποιεί ποσοστό του ΤΡΕΧΟΝΤΟΣ διαθέσιμου
      κεφαλαίου (όχι σταθερό ποσό).

ΔΙΑΧΕΙΡΙΣΗ ΡΙΣΚΟΥ
    - Leverage 1x, Isolated margin (ρητή εντολή στο API πριν κάθε άνοιγμα).
    - Stop Loss 5% / Take Profit 15% από την τιμή εισόδου (Risk-to-Reward 1:3).
    - SL/TP τοποθετούνται ΑΜΕΣΩΣ ως bracket orders (reduce-only) στο exchange,
      ώστε να εκτελούνται ακόμη κι αν το bot αποσυνδεθεί.

ΑΣΦΑΛΕΙΑ
    - API keys φορτώνονται ΜΟΝΟ μέσω αρχείου .env (ποτέ hardcoded).
    - Λειτουργία testnet (paper trading) εξ ορισμού — μηδενικό ρίσκο.
    - Error handling με retries/exponential backoff για αποσυνδέσεις API.

ΧΡΗΣΗ
    python trading_bot.py            # κανονική λειτουργία (paper/live ανά .env)
    python trading_bot.py --check    # διαγνωστικός έλεγχος (read-only, χωρίς orders)
    python trading_bot.py --selftest # offline έλεγχος λογικής σήματος (χωρίς API)

ΠΡΟΣΟΧΗ: Εκπαιδευτικός κώδικας — όχι επενδυτική συμβουλή.
Δοκιμάστε ΠΑΝΤΑ στο testnet πριν από οποιοδήποτε live trading.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import ccxt
import pandas as pd
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging — ελληνικά μηνύματα, ταυτόχρονα σε κονσόλα και αρχείο bot.log
# ---------------------------------------------------------------------------
log = logging.getLogger("bot")


def setup_logging(logfile: str = "bot.log") -> None:
    """Ρυθμίζει το logging με χρονοσφραγίδα, σε κονσόλα και (αν γίνεται) αρχείο."""
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    except OSError:
        # Αν δεν μπορούμε να γράψουμε αρχείο, συνεχίζουμε μόνο με κονσόλα.
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, handlers=handlers)


# ---------------------------------------------------------------------------
# Ρυθμίσεις — φορτώνονται από το .env (με τις τιμές του spec ως defaults)
# ---------------------------------------------------------------------------
@dataclass
class Config:
    exchange_id: str
    api_key: str
    api_secret: str
    use_testnet: bool
    symbols: list[str]
    timeframe: str
    ema_fast: int
    ema_slow: int
    leverage: int
    stop_loss_pct: float
    take_profit_pct: float
    position_size_pct: float
    loop_interval: int
    quote: str = "USDT"

    @classmethod
    def from_env(cls) -> "Config":
        """Διαβάζει τις μεταβλητές περιβάλλοντος από το αρχείο .env."""
        load_dotenv()

        def _bool(name: str, default: str) -> bool:
            return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y", "on")

        def _num(cast, name: str, default: str):
            raw = os.getenv(name, default).strip()
            try:
                return cast(raw)
            except ValueError:
                raise SystemExit(f"Μη έγκυρη τιμή στο .env: {name}='{raw}' (αναμένεται αριθμός).")

        symbols = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT").split(",") if s.strip()]
        return cls(
            exchange_id=os.getenv("EXCHANGE", "binance").strip(),
            api_key=os.getenv("API_KEY", "").strip(),
            api_secret=os.getenv("API_SECRET", "").strip(),
            use_testnet=_bool("USE_TESTNET", "true"),
            symbols=symbols,
            timeframe=os.getenv("TIMEFRAME", "1d").strip(),
            ema_fast=_num(int, "EMA_FAST", "9"),
            ema_slow=_num(int, "EMA_SLOW", "21"),
            leverage=_num(int, "LEVERAGE", "1"),
            stop_loss_pct=_num(float, "STOP_LOSS_PCT", "0.05"),
            take_profit_pct=_num(float, "TAKE_PROFIT_PCT", "0.15"),
            position_size_pct=_num(float, "POSITION_SIZE_PCT", "0.95"),
            loop_interval=_num(int, "LOOP_INTERVAL", "60"),
        )


# ---------------------------------------------------------------------------
# Δείκτες & ανίχνευση σήματος (καθαρές συναρτήσεις — εύκολα testable)
# ---------------------------------------------------------------------------
def compute_emas(closes: list[float], fast: int, slow: int) -> tuple[pd.Series, pd.Series]:
    """Υπολογίζει τον γρήγορο και τον αργό EMA (στάνταρ εκθετικός, adjust=False)."""
    s = pd.Series(closes, dtype="float64")
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    return ema_fast, ema_slow


def detect_signal(closes: list[float], fast: int, slow: int) -> Optional[str]:
    """
    Εντοπίζει *φρέσκια* διασταύρωση στα δύο τελευταία ΚΛΕΙΣΜΕΝΑ κεριά.

    Προσοχή: η λίστα `closes` πρέπει να περιέχει ΜΟΝΟ κλεισμένα κεριά — το τρέχον
    (ανοιχτό) κερί πρέπει να έχει ήδη αφαιρεθεί από τον caller.

    Επιστρέφει: "long", "short" ή None.
    """
    if len(closes) < slow + 2:
        return None
    ema_fast, ema_slow = compute_emas(closes, fast, slow)
    prev_fast, prev_slow = ema_fast.iloc[-2], ema_slow.iloc[-2]
    last_fast, last_slow = ema_fast.iloc[-1], ema_slow.iloc[-1]

    if prev_fast <= prev_slow and last_fast > last_slow:
        return "long"
    if prev_fast >= prev_slow and last_fast < last_slow:
        return "short"
    return None


# ---------------------------------------------------------------------------
# Wrapper γύρω από το CCXT exchange — με retries/backoff & testnet
# ---------------------------------------------------------------------------
class ExchangeClient:
    RETRY_DELAYS = (2, 4, 8, 16)  # exponential backoff (δευτερόλεπτα)
    RETRYABLE = (
        ccxt.NetworkError,
        ccxt.RequestTimeout,
        ccxt.ExchangeNotAvailable,
        ccxt.DDoSProtection,
    )

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = self._build_exchange()
        self.markets = self.safe_call(self.exchange.load_markets)
        log.info(
            "Φορτώθηκαν %d αγορές από '%s'%s.",
            len(self.markets), cfg.exchange_id, " [TESTNET]" if cfg.use_testnet else " [LIVE]",
        )

    def _build_exchange(self):
        try:
            klass = getattr(ccxt, self.cfg.exchange_id)
        except AttributeError:
            raise SystemExit(f"Άγνωστο exchange: '{self.cfg.exchange_id}'")
        # Binance USDⓂ futures -> 'future'. Άλλα exchanges (π.χ. bybit) -> 'swap'.
        default_type = "future" if self.cfg.exchange_id.startswith("binance") else "swap"
        ex = klass({
            "apiKey": self.cfg.api_key,
            "secret": self.cfg.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": default_type},
        })
        if self.cfg.use_testnet:
            # Το παλιό Binance futures testnet καταργήθηκε από τη ccxt· τα
            # Binance exchanges περνούν στο νέο Demo Trading (keys από το
            # https://demo.binance.com). Τα υπόλοιπα κρατούν το sandbox mode.
            if self.cfg.exchange_id.startswith("binance") and hasattr(ex, "enable_demo_trading"):
                ex.enable_demo_trading(True)
            else:
                ex.set_sandbox_mode(True)  # paper trading
        return ex

    def safe_call(self, fn, *args, **kwargs):
        """Εκτελεί κλήση API με αυτόματες επαναλήψεις σε σφάλματα δικτύου."""
        last_exc: Optional[Exception] = None
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            try:
                return fn(*args, **kwargs)
            except self.RETRYABLE as exc:
                last_exc = exc
                if attempt < len(self.RETRY_DELAYS):
                    delay = self.RETRY_DELAYS[attempt]
                    log.warning(
                        "Πρόβλημα σύνδεσης με το API (%s). Νέα προσπάθεια σε %ds [%d/%d]...",
                        type(exc).__name__, delay, attempt + 1, len(self.RETRY_DELAYS),
                    )
                    time.sleep(delay)
                else:
                    log.error(
                        "Η σύνδεση με το API απέτυχε μετά από %d προσπάθειες: %s",
                        len(self.RETRY_DELAYS), exc,
                    )
                    raise
        raise last_exc  # pragma: no cover (δεν φτάνει ποτέ εδώ)

    def place_order(self, symbol: str, order_type: str, side: str, amount: float,
                    price: Optional[float] = None, params: Optional[dict] = None):
        """create_order με idempotency key (clientOrderId).

        ΠΡΟΣΟΧΗ: το create_order ΔΕΝ είναι idempotent — αν γίνει timeout αφού η
        εντολή εκτελεστεί στο exchange, ένα τυφλό retry θα άνοιγε ΔΙΠΛΗ θέση.
        Εδώ, πριν από κάθε retry, ελέγχουμε με το clientOrderId αν η εντολή
        έχει ήδη περάσει· αν ναι, την επιστρέφουμε αντί να ξαναστείλουμε.
        """
        params = dict(params or {})
        cid = params.get("newClientOrderId") or f"bot-{uuid.uuid4().hex[:24]}"
        params["newClientOrderId"] = cid
        last_exc: Optional[Exception] = None
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            try:
                return self.exchange.create_order(symbol, order_type, side, amount, price, params)
            except self.RETRYABLE as exc:
                last_exc = exc
                existing = self._find_order_by_client_id(cid, symbol)
                if existing is not None:
                    log.warning(
                        "Η εντολή είχε ήδη εκτελεστεί στο exchange (clientOrderId=%s) — δεν ξαναστέλνεται.", cid
                    )
                    return existing
                if attempt < len(self.RETRY_DELAYS):
                    delay = self.RETRY_DELAYS[attempt]
                    log.warning(
                        "Πρόβλημα σύνδεσης στην αποστολή εντολής (%s). Νέα προσπάθεια σε %ds [%d/%d]...",
                        type(exc).__name__, delay, attempt + 1, len(self.RETRY_DELAYS),
                    )
                    time.sleep(delay)
                else:
                    log.error("Η αποστολή εντολής απέτυχε μετά από %d προσπάθειες: %s",
                              len(self.RETRY_DELAYS), exc)
                    raise
        raise last_exc  # pragma: no cover

    def _find_order_by_client_id(self, cid: str, symbol: str) -> Optional[dict]:
        """Ψάχνει εντολή με βάση το clientOrderId (None αν δεν υπάρχει/δεν βρεθεί)."""
        try:
            return self.exchange.fetch_order(None, symbol, {"origClientOrderId": cid})
        except Exception:
            return None

    def resolve_symbol(self, symbol: str) -> str:
        """Αναλύει το σύμβολο στη μορφή linear perpetual (π.χ. BTC/USDT:USDT)."""
        perp = f"{symbol}:{self.cfg.quote}"
        if perp in self.markets:
            return perp
        if symbol in self.markets:
            return symbol
        raise SystemExit(
            f"Το σύμβολο '{symbol}' δεν βρέθηκε στις αγορές futures του '{self.cfg.exchange_id}'."
        )


# ---------------------------------------------------------------------------
# Ο πυρήνας: state machine (FLAT ↔ IN_POSITION), entries, SL/TP, monitoring
# ---------------------------------------------------------------------------
class TradingBot:
    STATE_FILE = "bot_state.json"

    def __init__(self, client: ExchangeClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        # Σταθερή σειρά σάρωσης -> ντετερμινιστικό First-Come-First-Served.
        self.symbols = [client.resolve_symbol(s) for s in cfg.symbols]
        self.position: Optional[dict] = None
        # Ανά σύμβολο: timestamp του κλεισμένου κεριού στο οποίο ενεργήσαμε
        # (αποτρέπει επανείσοδο στο ίδιο σήμα μέσα στην ίδια ημέρα).
        # Διατηρείται σε αρχείο ώστε ένα restart να μην ξανα-ανοίξει θέση
        # στο ίδιο σήμα της ίδιας ημέρας.
        self.last_acted_ts: dict[str, int] = self._load_state()

    def _load_state(self) -> dict[str, int]:
        try:
            with open(self.STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {k: int(v) for k, v in data.get("last_acted_ts", {}).items()}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            with open(self.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_acted_ts": self.last_acted_ts}, f)
        except OSError as exc:
            log.warning("Αδυναμία αποθήκευσης κατάστασης στο %s: %s", self.STATE_FILE, exc)

    # ---- Βοηθητικά (read) -------------------------------------------------
    def free_balance(self) -> float:
        """Διαθέσιμο (free) υπόλοιπο στο quote νόμισμα (π.χ. USDT)."""
        bal = self.client.safe_call(self.client.exchange.fetch_balance)
        quote = bal.get(self.cfg.quote, {})
        return float(quote.get("free") or 0.0)

    def get_open_position(self, symbol: str) -> Optional[dict]:
        """Επιστρέφει την ανοιχτή θέση για το σύμβολο, ή None αν δεν υπάρχει."""
        positions = self.client.safe_call(self.client.exchange.fetch_positions, [symbol])
        for p in positions:
            if abs(float(p.get("contracts") or 0)) > 0:
                return p
        return None

    def closed_candles(self, symbol: str) -> tuple[list[float], Optional[int]]:
        """Κατεβάζει OHLCV και επιστρέφει τα closes ΜΟΝΟ των κλεισμένων κεριών."""
        ohlcv = self.client.safe_call(
            self.client.exchange.fetch_ohlcv, symbol, self.cfg.timeframe, None, 100
        )
        if not ohlcv or len(ohlcv) < 3:
            return [], None
        # Το τελευταίο κερί είναι το τρέχον (ανοιχτό) -> το αφαιρούμε.
        closed = ohlcv[:-1]
        closes = [float(c[4]) for c in closed]
        last_closed_ts = int(closed[-1][0])
        return closes, last_closed_ts

    # ---- Σάρωση & είσοδος -------------------------------------------------
    def scan_and_maybe_enter(self) -> None:
        """Σαρώνει όλα τα assets και μπαίνει στο πρώτο με έγκυρο σήμα (FCFS)."""
        balance = self.free_balance()
        log.info(
            "Αναμονή για σήμα... | Διαθέσιμο κεφάλαιο: %.2f %s | Σάρωση %d assets στο %s timeframe.",
            balance, self.cfg.quote, len(self.symbols), self.cfg.timeframe,
        )
        for symbol in self.symbols:
            log.info("Έλεγχος %s ...", symbol)
            closes, candle_ts = self.closed_candles(symbol)
            if not closes:
                log.warning("Ανεπαρκή δεδομένα για %s — παράλειψη.", symbol)
                continue

            signal = detect_signal(closes, self.cfg.ema_fast, self.cfg.ema_slow)
            ema_fast, ema_slow = compute_emas(closes, self.cfg.ema_fast, self.cfg.ema_slow)
            log.info(
                "   %s | τιμή=%.4f | EMA%d=%.4f | EMA%d=%.4f | σήμα: %s",
                symbol, closes[-1], self.cfg.ema_fast, ema_fast.iloc[-1],
                self.cfg.ema_slow, ema_slow.iloc[-1], signal or "κανένα",
            )

            if signal is None:
                continue
            if self.last_acted_ts.get(symbol) == candle_ts:
                log.info("   Έχουμε ήδη ενεργήσει σε αυτό το κερί για %s — αναμονή νέου.", symbol)
                continue

            log.info("➡️  Σήμα %s εντοπίστηκε στο %s! Προσπάθεια ανοίγματος θέσης (FCFS).",
                     signal.upper(), symbol)
            if self.enter_position(symbol, signal, candle_ts):
                return  # Μπήκαμε σε θέση -> σταματάμε τη σάρωση των υπολοίπων.

    def _last_price(self, symbol: str) -> Optional[float]:
        """Τελευταία τιμή με προστασία από ελλιπές ticker (last=None σε ρηχές αγορές)."""
        ticker = self.client.safe_call(self.client.exchange.fetch_ticker, symbol)
        last = ticker.get("last") or ticker.get("close") or (ticker.get("info") or {}).get("lastPrice")
        return float(last) if last else None

    def enter_position(self, symbol: str, side: str, candle_ts: int) -> bool:
        """Ανοίγει θέση με market order και τοποθετεί αμέσως SL/TP. True αν επιτύχει."""
        ex = self.client.exchange

        # 1) Ρητός ορισμός Isolated margin + leverage (π.χ. 1x) ΠΡΙΝ το άνοιγμα.
        #    Αν αποτύχει, ΔΕΝ ανοίγουμε θέση — αλλιώς θα τρέχαμε με ό,τι margin
        #    mode/leverage είχε μείνει ρυθμισμένο στον λογαριασμό.
        if not self._set_isolated_leverage(symbol):
            log.error("Ακύρωση εισόδου στο %s: δεν επιβεβαιώθηκε ISOLATED %dx.",
                      symbol, self.cfg.leverage)
            return False

        # 2) Μέγεθος θέσης (compounding: ποσοστό του τρέχοντος διαθέσιμου κεφαλαίου).
        balance = self.free_balance()
        price = self._last_price(symbol)
        if price is None or price <= 0:
            log.error("Μη διαθέσιμη τιμή για %s — παράλειψη εισόδου.", symbol)
            return False
        notional = balance * self.cfg.position_size_pct * self.cfg.leverage
        amount = float(ex.amount_to_precision(symbol, notional / price))
        if not self._validate_amount(symbol, amount, price):
            return False

        order_side = "buy" if side == "long" else "sell"
        log.info(
            "Τοποθέτηση MARKET %s: %.6f %s (~%.2f %s @ ~%.4f)...",
            order_side.upper(), amount, symbol, amount * price, self.cfg.quote, price,
        )
        try:
            order = self.client.place_order(symbol, "market", order_side, amount)
        except ccxt.InsufficientFunds as exc:
            log.error("Ανεπαρκές υπόλοιπο για άνοιγμα θέσης: %s", exc)
            return False
        except ccxt.ExchangeError as exc:
            log.error("Απόρριψη εντολής εισόδου από το exchange: %s", exc)
            return False

        # Από εδώ και πέρα η εντολή ΕΧΕΙ σταλεί: ό,τι κι αν δείξουν τα επόμενα
        # API calls, δεν επιτρέπεται νέα είσοδος στο ίδιο κερί.
        self.last_acted_ts[symbol] = candle_ts
        self._save_state()

        # 3) Επιβεβαίωση θέσης & πραγματική τιμή εισόδου. Πρωτεύουσα πηγή το
        #    response της εντολής (τα market orders επιστρέφουν fill αμέσως)·
        #    το fetch_positions μπορεί να αργεί να ενημερωθεί.
        order_filled = float(order.get("filled") or 0.0)
        order_avg = float(order.get("average") or 0.0)
        pos = self._wait_for_position(symbol)
        if pos is not None:
            entry = float(pos.get("entryPrice") or order_avg or price)
            filled = abs(float(pos.get("contracts") or order_filled or amount))
        elif order_filled > 0:
            log.warning("Η θέση δεν εμφανίστηκε ακόμη στο fetch_positions — "
                        "συνέχεια με τα στοιχεία εκτέλεσης της εντολής.")
            entry = order_avg or price
            filled = order_filled
        else:
            log.error("Δεν επιβεβαιώθηκε ούτε θέση ούτε εκτέλεση για %s — "
                      "δεν θα επιχειρηθεί ξανά στο ίδιο κερί.", symbol)
            return False

        # 4) Υπολογισμός & άμεση τοποθέτηση Stop Loss / Take Profit.
        sl, tp = self._sl_tp_prices(side, entry)
        sl = float(ex.price_to_precision(symbol, sl))
        tp = float(ex.price_to_precision(symbol, tp))
        sl_id, tp_id = self._place_brackets(symbol, side, filled, sl, tp)

        self.position = {
            "symbol": symbol, "side": side, "amount": filled, "entry": entry,
            "sl": sl, "tp": tp, "sl_id": sl_id, "tp_id": tp_id,
        }
        log.info(
            "✅ Θέση %s ΑΝΟΙΧΤΗ: %s | είσοδος=%.4f | SL=%.4f (-%.0f%%) | TP=%.4f (+%.0f%%)",
            side.upper(), symbol, entry, sl, self.cfg.stop_loss_pct * 100,
            tp, self.cfg.take_profit_pct * 100,
        )
        return True

    # ---- Παρακολούθηση ανοιχτής θέσης ------------------------------------
    def manage_open_position(self) -> None:
        """Παρακολουθεί την ανοιχτή θέση· εντοπίζει κλείσιμο από SL/TP."""
        p = self.position
        assert p is not None
        symbol = p["symbol"]

        pos = self.get_open_position(symbol)
        if pos is None:
            # Η θέση έκλεισε -> εντοπισμός αιτίας (SL/TP) & καθαρισμός.
            self._handle_closed_position()
            return

        mark = pos.get("markPrice") or self._last_price(symbol)
        if mark is None:
            log.warning("Μη διαθέσιμη τιμή mark για %s — παράλειψη κύκλου παρακολούθησης.", symbol)
            return
        mark = float(mark)
        pnl = float(pos.get("unrealizedPnl") or 0.0)
        log.info(
            "Θέση %s %s ανοιχτή | mark=%.4f | είσοδος=%.4f | SL=%.4f | TP=%.4f | μη υλοποιημένο PnL=%.2f %s",
            p["side"].upper(), symbol, mark, p["entry"], p["sl"], p["tp"], pnl, self.cfg.quote,
        )
        # Αν λείπει σκέλος SL/TP (αποτυχία στην τοποθέτηση), ξαναπροσπάθησε τώρα.
        self._ensure_brackets(p)
        # Backup safety-net: αν εξακολουθούν να λείπουν, κλείσε χειροκίνητα στο όριο.
        self._safety_net(symbol, p, mark)

    def _handle_closed_position(self) -> None:
        p = self.position
        assert p is not None
        symbol = p["symbol"]
        reason = self._determine_exit_reason(p)
        if reason == "tp":
            log.info("🎯 Take Profit χτυπήθηκε στο %s! Η θέση έκλεισε με κέρδος.", symbol)
        elif reason == "sl":
            log.info("🛑 Stop Loss χτυπήθηκε στο %s. Η θέση έκλεισε με ζημία.", symbol)
        else:
            log.info("Η θέση στο %s έκλεισε.", symbol)

        # Ακύρωση του εναπομείναντος bracket order (το άλλο σκέλος).
        for oid in (p.get("sl_id"), p.get("tp_id")):
            self._cancel_if_open(symbol, oid)

        self.position = None
        log.info("Επιστροφή σε αναζήτηση σήματος. Νέο διαθέσιμο κεφάλαιο: %.2f %s.",
                 self.free_balance(), self.cfg.quote)

    # ---- Εσωτερικά βοηθητικά ---------------------------------------------
    def _set_isolated_leverage(self, symbol: str) -> bool:
        """Ορίζει ISOLATED margin & το επιθυμητό leverage (graceful σε 'already set').

        Επιστρέφει False σε πραγματική αποτυχία — ο caller ΔΕΝ πρέπει τότε να
        ανοίξει θέση, αλλιώς θα τρέξει με άγνωστο margin mode/leverage.
        """
        ex = self.client.exchange
        try:
            self.client.safe_call(ex.set_margin_mode, "isolated", symbol)
            log.info("Margin mode -> ISOLATED για %s.", symbol)
        except ccxt.MarginModeAlreadySet:
            log.info("Margin mode ήδη ISOLATED για %s.", symbol)
        except ccxt.ExchangeError as exc:
            if "No need to change" in str(exc) or "-4046" in str(exc):
                log.info("Margin mode ήδη ISOLATED για %s.", symbol)
            else:
                log.error("Αδυναμία ορισμού margin mode για %s: %s", symbol, exc)
                return False
        try:
            self.client.safe_call(ex.set_leverage, self.cfg.leverage, symbol)
            log.info("Leverage -> %dx για %s.", self.cfg.leverage, symbol)
        except ccxt.ExchangeError as exc:
            if "not modified" in str(exc).lower():
                log.info("Leverage ήδη %dx για %s.", self.cfg.leverage, symbol)
            else:
                log.error("Αδυναμία ορισμού leverage για %s: %s", symbol, exc)
                return False
        return True

    def _validate_amount(self, symbol: str, amount: float, price: float) -> bool:
        """Ελέγχει το μέγεθος θέσης έναντι των ελάχιστων ορίων της αγοράς."""
        limits = self.client.markets[symbol].get("limits", {})
        min_amount = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        if amount <= 0:
            log.error("Μη έγκυρο μέγεθος θέσης (%.8f) για %s.", amount, symbol)
            return False
        if min_amount and amount < min_amount:
            log.error("Μέγεθος %.8f < ελάχιστο %.8f για %s.", amount, min_amount, symbol)
            return False
        if min_cost and amount * price < min_cost:
            log.error("Αξία θέσης %.2f < ελάχιστη %.2f %s για %s.",
                      amount * price, min_cost, self.cfg.quote, symbol)
            return False
        return True

    def _sl_tp_prices(self, side: str, entry: float) -> tuple[float, float]:
        """Υπολογίζει τις τιμές Stop Loss / Take Profit από την τιμή εισόδου."""
        if side == "long":
            return entry * (1 - self.cfg.stop_loss_pct), entry * (1 + self.cfg.take_profit_pct)
        return entry * (1 + self.cfg.stop_loss_pct), entry * (1 - self.cfg.take_profit_pct)

    def _place_single_bracket(self, symbol: str, order_type: str, exit_side: str,
                              amount: float, stop_price: float, name: str) -> Optional[str]:
        """Τοποθετεί ένα reduce-only σκέλος SL/TP. Επιστρέφει order id ή None."""
        try:
            order = self.client.place_order(
                symbol, order_type, exit_side, amount, None,
                {"stopPrice": stop_price, "reduceOnly": True},
            )
            oid = order.get("id")
            log.info("   Τοποθετήθηκε %s @ %.4f (id=%s).", name, stop_price, oid)
            return oid
        except ccxt.ExchangeError as exc:
            log.error("   Αποτυχία τοποθέτησης %s: %s", name, exc)
            return None

    def _place_brackets(self, symbol: str, side: str, amount: float,
                        sl: float, tp: float) -> tuple[Optional[str], Optional[str]]:
        """Τοποθετεί reduce-only Stop Loss (STOP_MARKET) & Take Profit (TAKE_PROFIT_MARKET)."""
        exit_side = "sell" if side == "long" else "buy"
        sl_id = self._place_single_bracket(symbol, "STOP_MARKET", exit_side, amount, sl, "STOP LOSS")
        tp_id = self._place_single_bracket(symbol, "TAKE_PROFIT_MARKET", exit_side, amount, tp, "TAKE PROFIT")
        return sl_id, tp_id

    def _ensure_brackets(self, p: dict) -> None:
        """Ξαναπροσπαθεί να τοποθετήσει όποιο σκέλος SL/TP λείπει (κάθε loop).

        Χωρίς αυτό, μια αποτυχία στο αρχικό _place_brackets θα άφηνε τη θέση
        μόνιμα χωρίς προστασία στο exchange, με μόνη άμυνα το safety net.
        """
        if p.get("sl_id") and p.get("tp_id"):
            return
        exit_side = "sell" if p["side"] == "long" else "buy"
        log.warning("Λείπει σκέλος SL/TP για %s — επαναπροσπάθεια τοποθέτησης.", p["symbol"])
        if not p.get("sl_id"):
            p["sl_id"] = self._place_single_bracket(
                p["symbol"], "STOP_MARKET", exit_side, p["amount"], p["sl"], "STOP LOSS")
        if not p.get("tp_id"):
            p["tp_id"] = self._place_single_bracket(
                p["symbol"], "TAKE_PROFIT_MARKET", exit_side, p["amount"], p["tp"], "TAKE PROFIT")

    def _wait_for_position(self, symbol: str, timeout: int = 10) -> Optional[dict]:
        """Περιμένει (μέχρι timeout) να εμφανιστεί η θέση μετά το market order."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            pos = self.get_open_position(symbol)
            if pos:
                return pos
            time.sleep(1)
        return self.get_open_position(symbol)

    def _determine_exit_reason(self, p: dict) -> str:
        """Καθορίζει αν η θέση έκλεισε από Stop Loss ή Take Profit."""
        ex = self.client.exchange
        for oid, reason in ((p.get("tp_id"), "tp"), (p.get("sl_id"), "sl")):
            if not oid:
                continue
            try:
                order = self.client.safe_call(ex.fetch_order, oid, p["symbol"])
            except ccxt.ExchangeError:
                continue
            status = (order.get("status") or "").lower()
            if status in ("closed", "filled") or float(order.get("filled") or 0) > 0:
                return reason
        # Fallback: σύγκριση τελευταίας τιμής με τα όρια SL/TP.
        try:
            last = float(self.client.safe_call(ex.fetch_ticker, p["symbol"])["last"])
        except Exception:
            return "unknown"
        if p["side"] == "long":
            if last >= p["tp"] * 0.999:
                return "tp"
            if last <= p["sl"] * 1.001:
                return "sl"
        else:
            if last <= p["tp"] * 1.001:
                return "tp"
            if last >= p["sl"] * 0.999:
                return "sl"
        return "unknown"

    def _cancel_if_open(self, symbol: str, oid: Optional[str]) -> None:
        """Ακυρώνει ένα order αν εξακολουθεί να υπάρχει (idempotent)."""
        if not oid:
            return
        try:
            self.client.safe_call(self.client.exchange.cancel_order, oid, symbol)
            log.info("   Ακυρώθηκε εναπομείναν order (id=%s).", oid)
        except ccxt.OrderNotFound:
            pass
        except ccxt.ExchangeError as exc:
            log.debug("   Το order id=%s δεν ακυρώθηκε (πιθανώς ήδη ανενεργό): %s", oid, exc)

    def _safety_net(self, symbol: str, p: dict, mark: float) -> None:
        """Εφεδρικό δίχτυ: κλείνει χειροκίνητα αν λείπουν τα bracket orders και παραβιαστεί όριο."""
        if p.get("sl_id") and p.get("tp_id"):
            return  # Υπάρχουν τα bracket orders -> το exchange χειρίζεται την έξοδο.
        breached = (
            (p["side"] == "long" and (mark <= p["sl"] or mark >= p["tp"])) or
            (p["side"] == "short" and (mark >= p["sl"] or mark <= p["tp"]))
        )
        if not breached:
            return
        log.warning("Backup safety-net: παραβιάστηκε όριο χωρίς ενεργό bracket order — χειροκίνητο κλείσιμο %s.", symbol)
        exit_side = "sell" if p["side"] == "long" else "buy"
        try:
            self.client.place_order(
                symbol, "market", exit_side, p["amount"], None, {"reduceOnly": True},
            )
        except ccxt.ExchangeError as exc:
            log.error("Αποτυχία χειροκίνητου κλεισίματος: %s", exc)

    # ---- Startup reconciliation ------------------------------------------
    def reconcile_on_startup(self) -> None:
        """Στην εκκίνηση: υιοθετεί τυχόν υπάρχουσα θέση & επανατοποθετεί SL/TP αν λείπουν."""
        log.info("Έλεγχος για υπάρχουσες ανοιχτές θέσεις (startup reconciliation)...")
        for symbol in self.symbols:
            pos = self.get_open_position(symbol)
            if not pos:
                continue
            side = pos.get("side") or "long"
            entry = float(pos.get("entryPrice") or 0.0)
            if entry <= 0:
                # Χωρίς entryPrice τα SL/TP θα έβγαιναν 0 (άκυρα orders, ψευδείς
                # έξοδοι). Fallback σε markPrice ή τελευταία τιμή.
                entry = float(pos.get("markPrice") or 0.0) or (self._last_price(symbol) or 0.0)
            if entry <= 0:
                log.error("Αδύνατος προσδιορισμός τιμής εισόδου για τη θέση %s — "
                          "ΔΕΝ τοποθετούνται SL/TP. Απαιτείται χειροκίνητος έλεγχος!", symbol)
                continue
            amount = abs(float(pos.get("contracts") or 0.0))
            sl, tp = self._sl_tp_prices(side, entry)
            sl = float(self.client.exchange.price_to_precision(symbol, sl))
            tp = float(self.client.exchange.price_to_precision(symbol, tp))

            sl_id, tp_id = self._find_existing_brackets(symbol)
            if not (sl_id and tp_id):
                log.warning("Βρέθηκε θέση %s χωρίς πλήρη SL/TP — επανατοποθέτηση.", symbol)
                sl_id, tp_id = self._place_brackets(symbol, side, amount, sl, tp)

            self.position = {
                "symbol": symbol, "side": side, "amount": amount, "entry": entry,
                "sl": sl, "tp": tp, "sl_id": sl_id, "tp_id": tp_id,
            }
            log.info("Υιοθετήθηκε υπάρχουσα θέση %s %s (είσοδος=%.4f). Συνέχιση παρακολούθησης.",
                     side.upper(), symbol, entry)
            return
        log.info("Δεν βρέθηκαν ανοιχτές θέσεις. Έναρξη σε κατάσταση FLAT.")

    def _find_existing_brackets(self, symbol: str) -> tuple[Optional[str], Optional[str]]:
        """Ψάχνει υπάρχοντα open orders για να εντοπίσει SL/TP της θέσης."""
        try:
            orders = self.client.safe_call(self.client.exchange.fetch_open_orders, symbol)
        except ccxt.ExchangeError:
            return None, None
        sl_id = tp_id = None
        for o in orders:
            otype = (o.get("type") or "").upper()
            if "TAKE_PROFIT" in otype:
                tp_id = o.get("id")
            elif "STOP" in otype:
                sl_id = o.get("id")
        return sl_id, tp_id

    # ---- Κύριος βρόχος ----------------------------------------------------
    def run(self) -> None:
        log.info("=== Εκκίνηση Trading Bot (EMA %d/%d crossover, %s) ===",
                 self.cfg.ema_fast, self.cfg.ema_slow, self.cfg.timeframe)
        log.info(
            "Assets: %s | Leverage: %dx ISOLATED | SL: %.0f%% | TP: %.0f%% | Loop: %ds",
            ", ".join(self.symbols), self.cfg.leverage, self.cfg.stop_loss_pct * 100,
            self.cfg.take_profit_pct * 100, self.cfg.loop_interval,
        )
        try:
            self.reconcile_on_startup()
        except ccxt.AuthenticationError as exc:
            log.error("Σφάλμα αυθεντικοποίησης στο startup (έλεγξε τα API keys): %s", exc)
            return

        while True:
            try:
                if self.position is None:
                    self.scan_and_maybe_enter()
                else:
                    self.manage_open_position()
            except ccxt.AuthenticationError as exc:
                log.error("Σφάλμα αυθεντικοποίησης (έλεγξε τα API keys): %s", exc)
                break
            except Exception as exc:  # noqa: BLE001 — ο βρόχος δεν πρέπει ΠΟΤΕ να πέφτει
                log.exception("Μη αναμενόμενο σφάλμα στον κύριο βρόχο: %s", exc)
            time.sleep(self.cfg.loop_interval)

    # ---- Διαγνωστικός έλεγχος (read-only) --------------------------------
    def run_check(self) -> None:
        log.info("=== Διαγνωστικός έλεγχος (read-only — δεν τοποθετούνται orders) ===")
        log.info("Διαθέσιμο υπόλοιπο: %.2f %s", self.free_balance(), self.cfg.quote)
        for symbol in self.symbols:
            closes, _ = self.closed_candles(symbol)
            if not closes:
                log.warning("%s: ανεπαρκή δεδομένα.", symbol)
                continue
            ema_fast, ema_slow = compute_emas(closes, self.cfg.ema_fast, self.cfg.ema_slow)
            signal = detect_signal(closes, self.cfg.ema_fast, self.cfg.ema_slow)
            log.info(
                "%s | τελευταίο κλείσιμο=%.4f | EMA%d=%.4f | EMA%d=%.4f | σήμα=%s",
                symbol, closes[-1], self.cfg.ema_fast, ema_fast.iloc[-1],
                self.cfg.ema_slow, ema_slow.iloc[-1], signal or "κανένα",
            )
        open_found = False
        for symbol in self.symbols:
            if self.get_open_position(symbol):
                log.info("Ανοιχτή θέση εντοπίστηκε: %s", symbol)
                open_found = True
        if not open_found:
            log.info("Καμία ανοιχτή θέση.")
        log.info("Ο διαγνωστικός έλεγχος ολοκληρώθηκε επιτυχώς.")


# ---------------------------------------------------------------------------
# Offline self-test της λογικής σήματος (δεν απαιτεί API keys ή δίκτυο)
# ---------------------------------------------------------------------------
def _linspace(start: float, stop: float, num: int) -> list[float]:
    if num <= 1:
        return [start]
    step = (stop - start) / (num - 1)
    return [start + step * i for i in range(num)]


def _truncate_to_first_cross(closes: list[float], fast: int, slow: int, direction: str) -> list[float]:
    """Κόβει τη σειρά ώστε η πρώτη διασταύρωση να βρίσκεται στο τελευταίο κερί."""
    ema_fast, ema_slow = compute_emas(closes, fast, slow)
    for i in range(1, len(closes)):
        prev_up = ema_fast.iloc[i - 1] <= ema_slow.iloc[i - 1]
        now_up = ema_fast.iloc[i] > ema_slow.iloc[i]
        if direction == "long" and prev_up and now_up:
            return closes[: i + 1]
        if direction == "short" and (not prev_up) and (not now_up) and \
                ema_fast.iloc[i - 1] >= ema_slow.iloc[i - 1] and ema_fast.iloc[i] < ema_slow.iloc[i]:
            return closes[: i + 1]
    return closes


def run_selftest() -> None:
    log.info("=== Offline self-test της detect_signal() ===")
    fast, slow = 9, 21

    # LONG: παρατεταμένη πτώση και απότομη ανοδική αντιστροφή.
    long_series = _truncate_to_first_cross(
        _linspace(120, 80, 40) + _linspace(81, 160, 15), fast, slow, "long"
    )
    # SHORT: παρατεταμένη άνοδος και απότομη καθοδική αντιστροφή.
    short_series = _truncate_to_first_cross(
        _linspace(80, 120, 40) + _linspace(119, 40, 15), fast, slow, "short"
    )
    # FLAT: σταθερή τιμή -> καμία διασταύρωση.
    flat_series = [100.0] * 60
    # Ανεπαρκή δεδομένα.
    short_data = [100.0, 101.0, 102.0]

    cases = [
        ("LONG cross", detect_signal(long_series, fast, slow), "long"),
        ("SHORT cross", detect_signal(short_series, fast, slow), "short"),
        ("FLAT (κανένα)", detect_signal(flat_series, fast, slow), None),
        ("Ανεπαρκή δεδομένα", detect_signal(short_data, fast, slow), None),
    ]
    failures = 0
    for name, got, expected in cases:
        ok = got == expected
        failures += 0 if ok else 1
        log.info("   [%s] %s -> got=%s, expected=%s", "OK" if ok else "FAIL", name, got, expected)

    if failures:
        log.error("Self-test ΑΠΕΤΥΧΕ με %d σφάλμα(τα).", failures)
        sys.exit(1)
    log.info("Self-test OK — όλες οι περιπτώσεις πέρασαν.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="EMA 9/21 crossover futures trading bot (CCXT)."
    )
    parser.add_argument("--check", action="store_true",
                        help="Διαγνωστικός έλεγχος (read-only, χωρίς orders).")
    parser.add_argument("--selftest", action="store_true",
                        help="Offline έλεγχος της λογικής σήματος (χωρίς API).")
    args = parser.parse_args()

    setup_logging()

    if args.selftest:
        run_selftest()
        return

    cfg = Config.from_env()
    if not cfg.api_key or not cfg.api_secret:
        log.error("Λείπουν τα API_KEY/API_SECRET. Συμπλήρωσέ τα στο αρχείο .env (δες .env.example).")
        sys.exit(1)

    client = ExchangeClient(cfg)
    bot = TradingBot(client, cfg)

    if args.check:
        bot.run_check()
        return

    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Τερματισμός από τον χρήστη. Τα bracket orders (SL/TP) παραμένουν "
                 "ενεργά στο exchange για προστασία τυχόν ανοιχτής θέσης.")


if __name__ == "__main__":
    main()
