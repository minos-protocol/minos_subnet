#!/usr/bin/env bash
# Start or restart the miner under PM2 (wraps start-miner.sh).
#
# Usage:
#   bash pm2-miner.sh          # live mode (uses .env wallet)
#   bash pm2-miner.sh --demo   # demo sandbox (no wallet, ephemeral keypair)
#
# --demo here just exports MINER_DEMO=true before PM2 takes over; the
# Python miner reads the env var at startup. To make demo mode persist
# across restarts add MINER_DEMO=true to your .env instead.
set -euo pipefail
cd "$(dirname "$0")"

DEMO=false
if [[ "${1:-}" == "--demo" ]]; then
  DEMO=true
  shift
fi

CONFIG="$(pwd)/ecosystem.miner.config.js"
if [[ ! -f "$CONFIG" ]]; then
  echo "Missing $CONFIG" >&2
  exit 1
fi

if ! command -v pm2 &>/dev/null; then
  echo "PM2 not found. Install with: npm install -g pm2" >&2
  echo "Or re-run: bash install.sh (installs Node + PM2 when needed)" >&2
  exit 1
fi

if [[ "$DEMO" == "true" ]]; then
  echo "Launching minos-miner under PM2 in DEMO mode (MINER_DEMO=true)"
  export MINER_DEMO=true
fi

if pm2 describe minos-miner &>/dev/null; then
  exec pm2 restart minos-miner --update-env
else
  exec pm2 start "$CONFIG"
fi
