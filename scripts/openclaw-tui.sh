#!/usr/bin/env bash
# Launch OpenClaw TUI using the local gateway token without printing it.
#
# Raw `openclaw tui` can ask for device/scope pairing on headless VMs. Minos
# configures a local loopback gateway token during assistant setup, so this
# wrapper hands that token to the TUI and keeps the first-use path smooth.
set -euo pipefail

config_path="$(openclaw config file 2>/dev/null || printf '%s/.openclaw/openclaw.json' "$HOME")"

token="$(
  python3 - "$config_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
try:
    data = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit

print(data.get("gateway", {}).get("auth", {}).get("token", ""))
PY
)"

if [[ -n "$token" ]]; then
  # Passing --token would put the gateway token on argv, where any local user
  # could read it from `ps -eo args` or /proc/<pid>/cmdline, defeating the
  # chmod 600 on openclaw.json. OpenClaw reads OPENCLAW_GATEWAY_TOKEN for the
  # configured gateway when no --url/host target is given, which is this
  # wrapper's local-loopback case.
  export OPENCLAW_GATEWAY_TOKEN="$token"
  exec openclaw tui "$@"
fi

echo "OpenClaw gateway token not found. Re-run:" >&2
echo "  bash scripts/setup_ai_assistant.sh --with-ditto --openclaw" >&2
exit 1
