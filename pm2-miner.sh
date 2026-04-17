#!/usr/bin/env bash
# Start or restart the miner under PM2 (wraps start-miner.sh).
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="$(pwd)/ecosystem.miner.config.js"
if [[ ! -f "$CONFIG" ]]; then
  echo "Missing $CONFIG" >&2
  exit 1
fi

if ! command -v pm2 &>/dev/null; then
  echo "PM2 not found. Install with: npm install -g pm2" >&2
  echo "Or re-run: bash install.sh (installs PM2 when npm is available)" >&2
  exit 1
fi

if pm2 describe minos-miner &>/dev/null; then
  exec pm2 restart minos-miner --update-env
else
  exec pm2 start "$CONFIG"
fi
