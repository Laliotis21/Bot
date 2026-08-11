# Crypto Futures Trading Bot — EMA 9/21 Crossover

Αυτόματο trading bot για **crypto futures** μέσω [CCXT](https://github.com/ccxt/ccxt),
με στρατηγική **διασταύρωσης EMA 9/21** στο **Daily (1d)** timeframe και αυστηρή
διαχείριση ρίσκου. Σχεδιασμένο να τρέχει **out-of-the-box στο Binance Futures Testnet**
(paper trading, μηδενικό ρίσκο).

> ⚠️ **Disclaimer:** Εκπαιδευτικός κώδικας — **όχι** επενδυτική συμβουλή. Το trading σε
> futures ενέχει ρίσκο απώλειας κεφαλαίου. Δοκίμασέ το **πάντα** στο testnet πριν από
> οποιοδήποτε live trading, και μόνο με κεφάλαιο που αντέχεις να χάσεις.

## Τι κάνει

| Κατηγορία | Προδιαγραφή |
|---|---|
| **Assets** | `BTC/USDT`, `ETH/USDT`, `SOL/USDT` (ρυθμιζόμενα) |
| **Timeframe** | Daily (1d) |
| **Σήμα Long** | EMA9 διασταυρώνεται **πάνω** από EMA21 στο κλείσιμο ημέρας |
| **Σήμα Short** | EMA9 διασταυρώνεται **κάτω** από EMA21 στο κλείσιμο ημέρας |
| **Επιλογή** | First-Come-First-Served — μία θέση τη φορά (σάρωση BTC → ETH → SOL) |
| **Compounding** | Κάθε trade χρησιμοποιεί ποσοστό του **τρέχοντος** διαθέσιμου κεφαλαίου |
| **Leverage** | 1x, **Isolated** (ρητή εντολή στο API πριν κάθε άνοιγμα) |
| **Stop Loss** | 5% από την τιμή εισόδου |
| **Take Profit** | 15% από την τιμή εισόδου (Risk-to-Reward 1:3) |
| **Έξοδος** | Bracket orders (reduce-only STOP_MARKET / TAKE_PROFIT_MARKET) στο exchange |

## Γιατί το 1x Isolated είναι ασφαλές

Στο **1x isolated**, ένα **long** ουσιαστικά δεν ρευστοποιείται από φυσιολογικές κινήσεις
(η τιμή θα έπρεπε να πάει ~στο 0), ενώ ένα **short** θα απαιτούσε ~+100% κίνηση για
liquidation. Το **Stop Loss στο 5%** ενεργοποιείται πολύ πριν από οποιοδήποτε σενάριο
liquidation — άρα ο κίνδυνος μηδενισμού πρακτικά εξαλείφεται.

## Εγκατάσταση

```bash
# 1) Virtual environment
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate

# 2) Εξαρτήσεις
pip install -r requirements.txt

# 3) Ρυθμίσεις & κλειδιά
cp .env.example .env
# άνοιξε το .env και συμπλήρωσε API_KEY / API_SECRET
```

### Κλειδιά για το Binance Futures Testnet

1. Πήγαινε στο **https://testnet.binancefuture.com** και σύνδεση (GitHub login).
2. Κάτω-δεξιά στο tab **API Key**, δημιούργησε κλειδί (testnet — εικονικά χρήματα).
3. Αντίγραψε `API Key` / `API Secret` στο `.env`. Άφησε `USE_TESTNET=true`.

## Εκτέλεση

```bash
# Offline έλεγχος της λογικής σήματος (δεν χρειάζεται API/δίκτυο)
python trading_bot.py --selftest

# Διαγνωστικός έλεγχος: σύνδεση, υπόλοιπο, EMA & τρέχον σήμα ανά asset (read-only)
python trading_bot.py --check

# Κανονική λειτουργία (paper ή live ανάλογα με το .env)
python trading_bot.py
```

Συνιστάται η σειρά: `--selftest` → `--check` → κανονική λειτουργία στο testnet.

## Πώς λειτουργεί ο βρόχος

- **Όσο είμαστε FLAT:** σαρώνει τα assets με τη σειρά, υπολογίζει EMA9/EMA21 στα
  **κλεισμένα** ημερήσια κεριά και μπαίνει στο **πρώτο** με έγκυρο σήμα. Μόλις ανοίξει
  θέση, ορίζει leverage/margin, υπολογίζει μέγεθος (compounding) και τοποθετεί **αμέσως**
  Stop Loss & Take Profit.
- **Όσο υπάρχει θέση:** παρακολουθεί την κατάσταση, καταγράφει PnL, και όταν χτυπήσει
  SL ή TP ακυρώνει το εναπομείναν σκέλος και επιστρέφει σε αναζήτηση σήματος.
- **Restart-safe:** στην εκκίνηση εντοπίζει τυχόν υπάρχουσα θέση και επανατοποθετεί
  SL/TP αν λείπουν.

Όλα τα μηνύματα είναι στα **Ελληνικά**, με χρονοσφραγίδα, και γράφονται σε κονσόλα + `bot.log`.

## Ρυθμίσεις (`.env`)

Όλες οι παράμετροι (assets, περίοδοι EMA, SL/TP %, leverage, μέγεθος θέσης, συχνότητα
βρόχου) ρυθμίζονται από το `.env`. Δες το [`.env.example`](.env.example) για επεξηγήσεις.

> Το spec ορίζει χρήση **100%** του κεφαλαίου· το default `POSITION_SIZE_PCT=0.95`
> αφήνει μικρό buffer για fees. Όρισέ το σε `1.0` για ακριβώς 100%.

## Μετάβαση σε Live

1. Δοκίμασε εκτενώς στο testnet.
2. Στο `.env`: `USE_TESTNET=false` και βάλε **live** API keys (με δικαιώματα Futures,
   χωρίς withdrawal, ιδανικά IP whitelist).
3. Ξεκίνα με μικρό κεφάλαιο.

## Δομή

```
trading_bot.py    # Όλη η λογική (config, indicators, exchange client, bot)
.env.example      # Template ρυθμίσεων
requirements.txt  # ccxt, pandas, python-dotenv
.gitignore        # αποκλείει το .env και logs
```

---

## Spot Framework (`app/`) — modular production system

A separate **Binance Spot** algorithmic trading framework lives under [`app/`](app/).
It does **not** replace the futures bots above.

See the full documentation: [`docs/SPOT_FRAMEWORK.md`](docs/SPOT_FRAMEWORK.md)

```bash
pip install -e ".[dev]"
python -m app.cli backtest
python -m app.cli monte-carlo --seed 42
python -m pytest
```

Default mode is **paper trading**. Live trading requires explicit confirmation.
