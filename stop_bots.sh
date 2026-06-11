#!/bin/bash
# Τερματισμός όλων των bots + dashboard που ξεκίνησαν με start_bots.sh.
cd "$(dirname "$0")"
for f in .pids/*.pid; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .pid)
    pid=$(cat "$f")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" && echo "⏹ $name σταμάτησε (PID $pid)"
    else
        echo "· $name δεν έτρεχε"
    fi
    rm -f "$f"
done
