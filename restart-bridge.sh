#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

REPO_ROOT="$(pwd)"
STATE_DIR="$REPO_ROOT/state"
ENV_FILE="$REPO_ROOT/.env"
RUN_SCRIPT="$REPO_ROOT/run-bridge.sh"
LOCAL_LOCK_FILE="$STATE_DIR/bridge.lock"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PREFERRED_LABEL="com.liao.telegram-codex-bridge"
PREFERRED_PLIST="$LAUNCH_AGENTS_DIR/$PREFERRED_LABEL.plist"

mkdir -p "$STATE_DIR"

log() {
  printf '[restart] %s\n' "$*"
}

plist_label() {
  /usr/libexec/PlistBuddy -c 'Print :Label' "$1" 2>/dev/null || true
}

plist_points_here() {
  local plist="$1"
  grep -Fq "$RUN_SCRIPT" "$plist"
}

trim_quotes() {
  local value="$1"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

telegram_lock_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    return 0
  fi
  local token
  token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -n 1 | cut -d= -f2- || true)"
  token="$(trim_quotes "$token")"
  if [[ -z "$token" ]]; then
    return 0
  fi
  local fingerprint
  fingerprint="$(printf '%s' "$token" | shasum -a 256 | awk '{print substr($1, 1, 16)}')"
  printf '%s/.telegram-bridge-locks/%s.lock\n' "$HOME" "$fingerprint"
}

match_plists=()
if [[ -d "$LAUNCH_AGENTS_DIR" ]]; then
  while IFS= read -r plist; do
    match_plists+=("$plist")
  done < <(find "$LAUNCH_AGENTS_DIR" -maxdepth 1 -name '*.plist' -print | sort)
fi

repo_plists=()
for plist in "${match_plists[@]}"; do
  if plist_points_here "$plist"; then
    repo_plists+=("$plist")
  fi
done

if [[ ${#repo_plists[@]} -eq 0 ]]; then
  log "No installed LaunchAgent points at $RUN_SCRIPT."
  log "Expected canonical plist: $PREFERRED_PLIST"
  exit 1
fi

canonical_plist=""
for plist in "${repo_plists[@]}"; do
  if [[ "$(plist_label "$plist")" == "$PREFERRED_LABEL" ]]; then
    canonical_plist="$plist"
    break
  fi
done
if [[ -z "$canonical_plist" ]]; then
  canonical_plist="${repo_plists[0]}"
fi
canonical_label="$(plist_label "$canonical_plist")"

if [[ -z "$canonical_label" ]]; then
  log "Could not determine LaunchAgent label for $canonical_plist"
  exit 1
fi

log "Canonical LaunchAgent: $canonical_label"

for plist in "${repo_plists[@]}"; do
  label="$(plist_label "$plist")"
  if [[ -n "$label" ]]; then
    log "Booting out $label"
  else
    log "Booting out unknown label from $plist"
  fi
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
done

sleep 1

if pids="$(ps -axo pid=,command= | grep -F "$REPO_ROOT" | grep -F "bridge.py" | awk '{print $1}')" && [[ -n "$pids" ]]; then
  log "Stopping leftover bridge processes: $pids"
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done <<< "$pids"
  sleep 2
fi

if pids="$(ps -axo pid=,command= | grep -F "$REPO_ROOT" | grep -F "bridge.py" | awk '{print $1}')" && [[ -n "$pids" ]]; then
  log "Force killing stubborn bridge processes: $pids"
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill -9 "$pid" >/dev/null 2>&1 || true
  done <<< "$pids"
  sleep 1
fi

if [[ -f "$LOCAL_LOCK_FILE" ]]; then
  log "Removing stale local lock $LOCAL_LOCK_FILE"
  rm -f "$LOCAL_LOCK_FILE"
fi

GLOBAL_LOCK_FILE="$(telegram_lock_file || true)"
if [[ -n "${GLOBAL_LOCK_FILE:-}" && -f "$GLOBAL_LOCK_FILE" ]]; then
  log "Removing stale Telegram token lock $GLOBAL_LOCK_FILE"
  rm -f "$GLOBAL_LOCK_FILE"
fi

for plist in "${repo_plists[@]}"; do
  if [[ "$plist" == "$canonical_plist" ]]; then
    continue
  fi
  log "Leaving duplicate plist disabled: $plist"
done

log "Bootstrapping $canonical_label"
launchctl bootstrap "gui/$(id -u)" "$canonical_plist"

log "Kickstarting $canonical_label"
launchctl kickstart -k "gui/$(id -u)/$canonical_label"

sleep 2

log "LaunchAgent status:"
launchctl print "gui/$(id -u)/$canonical_label" | sed -n '1,40p'

if [[ -f "$LOCAL_LOCK_FILE" ]]; then
  log "Current bridge.lock PID: $(cat "$LOCAL_LOCK_FILE")"
else
  log "Warning: $LOCAL_LOCK_FILE was not recreated yet."
fi
