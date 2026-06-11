#!/bin/bash
# Εκκίνηση όλων των bots + dashboard σε background (ανεξάρτητα από terminal).
# Χρήση: ./start_bots.sh   |   Τερματισμός: ./stop_bots.sh
cd "$(dirname "$0")"
mkdir -p .pids

start() {
    local name="$1"; shift
    if [ -f ".pids/$name.pid" ] && kill -0 "$(cat ".pids/$name.pid")" 2>/dev/null; then
        echo "✓ $name τρέχει ήδη (PID $(cat ".pids/$name.pid"))"
        return
    fi
    nohup "$@" >> "$name.out" 2>&1 &
    echo $! > ".pids/$name.pid"
    echo "▶ $name ξεκίνησε (PID $!)"
}

start ema       python3 trading_bot.py
start tsmom     python3 tsmom_bot.py
start funding   python3 funding_bot.py
start dashboard python3 dashboard/server.py --port 8000

echo
echo "Dashboard: http://localhost:8000"
echo "Logs: bot.log, tsmom.log, funding.log"
