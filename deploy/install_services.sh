#!/bin/bash
# Εγκατάσταση systemd services για τα bots (Linux server / VPS).
# - Ξεκινούν αυτόματα στο boot
# - Επανεκκινούν μόνα τους αν crashάρουν
#
# Χρήση (μέσα στον φάκελο του repo, στον server):
#   sudo bash deploy/install_services.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
RUN_USER="${SUDO_USER:-$(whoami)}"

declare -A BOTS=(
    [trading-ema]="$PYTHON $REPO_DIR/trading_bot.py"
    [trading-tsmom]="$PYTHON $REPO_DIR/tsmom_bot.py"
    [trading-funding]="$PYTHON $REPO_DIR/funding_bot.py"
    [trading-dashboard]="$PYTHON $REPO_DIR/dashboard/server.py --port 8000"
)

for name in "${!BOTS[@]}"; do
    cat > "/etc/systemd/system/$name.service" <<UNIT
[Unit]
Description=$name (crypto trading bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=${BOTS[$name]}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --now "$name"
    echo "✓ $name: εγκαταστάθηκε & ξεκίνησε"
done

echo
echo "Έλεγχος:   systemctl status trading-ema trading-tsmom trading-funding trading-dashboard"
echo "Logs:      journalctl -u trading-ema -f   (ή tail -f $REPO_DIR/bot.log)"
echo "Dashboard: ssh -L 8000:localhost:8000 $RUN_USER@<server-ip>  ->  http://localhost:8000"
