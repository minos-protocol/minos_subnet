#!/usr/bin/env bash
# Start or restart the LIVE miner under PM2 (wraps start-miner.sh).
#
# Usage:
#   bash pm2-miner.sh          # live mode (uses .env wallet)
#
# PM2 is for the long-running live miner only. Demo/practice are one-shot
# scoring tools that run once and exit — run those in the foreground
# (`bash start-miner.sh --demo` / `--practice`), NOT under a process
# supervisor, which would restart-loop them.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
Usage: bash pm2-miner.sh [OPTIONS]

Runs the LIVE miner under PM2. For a one-shot scored test run without a
wallet, use: bash start-miner.sh --demo  (or --practice).

Options:
  --help, -h   Show this help.

Examples:
  bash pm2-miner.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

CONFIG="$(pwd)/ecosystem.miner.config.js"
if [[ ! -f "$CONFIG" ]]; then
  echo "Missing $CONFIG" >&2
  exit 1
fi

if ! command -v pm2 &>/dev/null; then
  echo "PM2 not found. Re-run: bash install.sh" >&2
  echo "The installer repairs Node/npm, installs PM2, and reports any PATH fix needed." >&2
  echo "Manual fallback: npm install -g pm2" >&2
  exit 1
fi

if pm2 describe minos-miner &>/dev/null; then
  exec pm2 restart minos-miner --update-env
else
  exec pm2 start "$CONFIG"
fi
