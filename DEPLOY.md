# Deployment — δωρεάν 24/7 hosting

Τα bots είναι long-running processes — θέλουν VM (όχι serverless όπως Vercel/Netlify).
Δύο **πραγματικά δωρεάν για πάντα** επιλογές:

## Επιλογή Α — Oracle Cloud «Always Free» (προτείνεται)

Δίνει δωρεάν για πάντα ARM VM έως 4 cores / 24GB RAM — υπεραρκετό.

1. Λογαριασμός στο https://signup.oraclecloud.com (ζητά κάρτα για ταυτοποίηση,
   ΔΕΝ χρεώνει — μείνε σε «Always Free» πόρους).
2. Compute → Create Instance → Image: **Ubuntu 22.04**, Shape: **VM.Standard.A1.Flex**
   (Always Free eligible). Κατέβασε το SSH key.
3. Σύνδεση: `ssh -i key.pem ubuntu@<server-ip>`

## Επιλογή Β — Google Cloud «Free Tier»

e2-micro VM δωρεάν για πάντα (us-west1/us-central1/us-east1, 1 vCPU/1GB —
αρκεί, τα bots είναι ελαφριά). console.cloud.google.com → Compute Engine →
Create Instance → e2-micro + Ubuntu 22.04.

## Εγκατάσταση (ίδια και στις δύο, ~5 λεπτά)

```bash
sudo apt update && sudo apt install -y python3-pip git
git clone https://github.com/Laliotis21/Bot.git && cd Bot
pip3 install -r requirements.txt

cp .env.example .env
nano .env                     # συμπλήρωσε API_KEY / API_SECRET

python3 trading_bot.py --check    # επαλήθευση σύνδεσης (read-only)
sudo bash deploy/install_services.sh
```

Αυτό εγκαθιστά 4 systemd services (trading-ema, trading-tsmom,
trading-funding, trading-dashboard) που:
- ξεκινούν αυτόματα σε κάθε boot του server
- επανεκκινούν μόνα τους σε crash (Restart=always, 30s)

## Dashboard — ασφαλής πρόσβαση

Ο server του dashboard ακούει ΜΟΝΟ σε localhost (δείχνει το υπόλοιπό σου —
δεν πρέπει να είναι δημόσιο). Από το laptop σου:

```bash
ssh -L 8000:localhost:8000 ubuntu@<server-ip>
# και μετά άνοιξε http://localhost:8000 στον browser σου
```

## Διαχείριση

```bash
systemctl status trading-ema                  # κατάσταση
journalctl -u trading-funding -f              # live logs μέσω systemd
sudo systemctl restart trading-tsmom          # επανεκκίνηση ενός bot
cd ~/Bot && git pull && sudo systemctl restart trading-{ema,tsmom,funding,dashboard}  # update
```

## Ασφάλεια

- `chmod 600 .env` — μόνο ο χρήστης σου διαβάζει τα keys.
- Τα τωρινά keys είναι **demo** (paper trading) — μηδενικό ρίσκο. Πριν από
  live keys: περιόρισέ τα στο IP του server (Binance API restrictions),
  ΧΩΡΙΣ δικαίωμα withdrawal, και κράτα το dashboard πίσω από SSH tunnel.
