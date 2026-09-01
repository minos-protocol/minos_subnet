#!/usr/bin/env bash
# Minos automatic git updater.
#
# This is started by ./enable_auto_update.sh under PM2 as "minos-auto-update".
# It checks the configured git branch, pulls only safe fast-forward updates,
# and restarts only the confirmed miner/validator PM2 process.
#
# Safety rules:
#   - Local git changes mean "skip update".
#   - Non-fast-forward branch changes mean "skip update".
#   - A missing PM2 process means "skip update".
#   - No git reset, no force pull, no overwriting user edits.

set -euo pipefail

CONFIG_FILE="${MINOS_AUTO_UPDATE_CONFIG:-${HOME}/.minos/auto_update.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Auto-update config not found: ${CONFIG_FILE}"
  echo "Run ./enable_auto_update.sh from your Minos repo first."
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

REPO="${MINOS_AUTO_UPDATE_REPO:?Missing MINOS_AUTO_UPDATE_REPO}"
REMOTE="${MINOS_AUTO_UPDATE_REMOTE:-origin}"
BRANCH="${MINOS_AUTO_UPDATE_BRANCH:-main}"
PM2_PROCESS="${MINOS_AUTO_UPDATE_PM2_PROCESS:?Missing MINOS_AUTO_UPDATE_PM2_PROCESS}"
INTERVAL_SECONDS="${MINOS_AUTO_UPDATE_INTERVAL_SECONDS:-300}"
ALLOW_DIRTY="${MINOS_AUTO_UPDATE_ALLOW_DIRTY:-0}"
LOG_FILE="${MINOS_AUTO_UPDATE_LOG:-${HOME}/.minos/auto_update.log}"
LOCK_DIR="${HOME}/.minos/auto_update.lock"
LOCK_MAX_AGE_MINUTES="${MINOS_AUTO_UPDATE_LOCK_MAX_AGE_MINUTES:-60}"
ACTIVE_LOCK="${MINOS_AUTO_UPDATE_ACTIVE_LOCK:-}"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "Missing required command: $1"
    return 1
  fi
}

# The lock dir is a fixed path, and the old RETURN-only trap never fired when
# pm2 restarted us or the OOM killer stopped us mid-check. The orphaned dir then
# made every later run bail out with "already running", so auto-update stopped
# forever while pm2 still reported the process online. Release on signals too,
# and reclaim a lock too old to belong to a live check.
LOCK_HELD=false

release_lock() {
  if [[ "$LOCK_HELD" == "true" ]]; then
    LOCK_HELD=false
    rm -rf "$LOCK_DIR"
  fi
}

trap release_lock EXIT
trap 'release_lock; exit 130' INT
trap 'release_lock; exit 143' TERM

lock_is_stale() {
  [[ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin "+${LOCK_MAX_AGE_MINUTES}" 2>/dev/null)" ]]
}

with_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if ! lock_is_stale; then
      log "Another auto-update check is already running. Skipping."
      return 0
    fi
    log "Lock ${LOCK_DIR} is older than ${LOCK_MAX_AGE_MINUTES}m; reclaiming it from a dead check."
    rm -rf "$LOCK_DIR"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      log "Another auto-update check is already running. Skipping."
      return 0
    fi
  fi
  LOCK_HELD=true
  trap release_lock RETURN
  check_once
}

repo_is_dirty() {
  [[ -n "$(git status --porcelain)" ]]
}

check_once() {
  require_command git || return 0
  require_command pm2 || return 0

  if [[ ! -d "$REPO/.git" ]]; then
    log "Repo not found or not a git checkout: $REPO"
    return 0
  fi

  cd "$REPO"

  current_branch="$(git branch --show-current)"
  if [[ "$current_branch" != "$BRANCH" ]]; then
    log "Current git branch is '${current_branch:-detached}', expected '${BRANCH}'. Skipping auto-update."
    return 0
  fi

  if ! pm2 describe "$PM2_PROCESS" >/dev/null 2>&1; then
    log "PM2 process '$PM2_PROCESS' not found. Run ./enable_auto_update.sh again if it was renamed."
    return 0
  fi

  if [[ -n "$ACTIVE_LOCK" && -e "$ACTIVE_LOCK" ]]; then
    log "Active work lock exists ($ACTIVE_LOCK). Skipping update."
    return 0
  fi

  if repo_is_dirty && [[ "$ALLOW_DIRTY" != "1" ]]; then
    log "Local git changes detected. Skipping auto-update to protect user edits."
    return 0
  fi

  if ! git fetch --quiet "$REMOTE" "$BRANCH"; then
    log "git fetch failed for ${REMOTE}/${BRANCH}."
    return 0
  fi

  local_sha="$(git rev-parse HEAD)"
  remote_sha="$(git rev-parse "${REMOTE}/${BRANCH}")"

  if [[ "$local_sha" == "$remote_sha" ]]; then
    log "Already up to date on ${BRANCH} (${local_sha:0:8})."
    return 0
  fi

  if ! git merge-base --is-ancestor HEAD "${REMOTE}/${BRANCH}"; then
    log "Remote branch is not a fast-forward from local HEAD. Manual update required."
    return 0
  fi

  log "Update available: ${local_sha:0:8} -> ${remote_sha:0:8}. Pulling ${REMOTE}/${BRANCH}."

  if ! git pull --ff-only "$REMOTE" "$BRANCH"; then
    log "git pull --ff-only failed. PM2 process was not restarted."
    return 0
  fi

  log "Restarting PM2 process: ${PM2_PROCESS}"
  if pm2 restart "$PM2_PROCESS" --update-env; then
    pm2 save >/dev/null 2>&1 || true
    log "Update applied and '${PM2_PROCESS}' restarted."
  else
    log "Update pulled, but PM2 restart failed for '${PM2_PROCESS}'. Check pm2 logs."
  fi
}

if [[ "${1:-}" == "--once" ]]; then
  with_lock
  exit 0
fi

log "Minos auto-update started for '${PM2_PROCESS}' on ${REMOTE}/${BRANCH}."
while true; do
  with_lock
  sleep "$INTERVAL_SECONDS"
done
