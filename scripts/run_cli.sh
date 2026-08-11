#!/usr/bin/env bash
# Offline paper/backtest helpers — never places live orders by default.
set -euo pipefail
cd "$(dirname "$0")/.."
export PAPER_TRADING=true
python -m app.cli "$@"
