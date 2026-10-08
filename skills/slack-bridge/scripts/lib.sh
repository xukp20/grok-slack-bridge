# shellcheck disable=SC2034  # variables are used by the scripts that source this file
# shellcheck shell=bash
# Shared helpers for the slack-bridge shell scripts. Source, do not execute.
#
# BRIDGE_HOME is the live install directory: $SLACK_BRIDGE_HOME if set,
# otherwise the parent of the scripts/ directory *as invoked* (so a symlinked
# /workspace/slack-bot/scripts resolves to /workspace/slack-bot).

set -euo pipefail

_caller="${BASH_SOURCE[1]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$_caller")" && pwd -L)"
CODE_DIR="$(cd "$(dirname "$(readlink -f "$_caller")")" && pwd -P)"
SKILL_DIR="$(cd "$CODE_DIR/.." && pwd -P)"
BRIDGE_HOME="${SLACK_BRIDGE_HOME:-$(cd "$SCRIPT_DIR/.." && pwd -L)}"
export SLACK_BRIDGE_HOME="$BRIDGE_HOME"

PY="$BRIDGE_HOME/.venv/bin/python"
RUN_DIR="$BRIDGE_HOME/run"
LOG_DIR="$BRIDGE_HOME/logs"
PIDFILE="$RUN_DIR/bridge.pid"
LOGFILE="$LOG_DIR/bridge.log"

REQUIRED_ENV=(SLACK_BOT_TOKEN SLACK_APP_TOKEN GROK_WEBHOOK_URL GROK_WEBHOOK_AUTH)

die() { echo "slack-bridge: $*" >&2; exit 2; }

require_install() {
  [[ -f "$BRIDGE_HOME/config.json" ]] || die "no config.json in $BRIDGE_HOME.
  Install first:  $SKILL_DIR/scripts/install.sh <install-dir>
  or point SLACK_BRIDGE_HOME at an existing install."
  [[ -x "$PY" ]] || die "missing virtualenv at $BRIDGE_HOME/.venv; re-run install.sh $BRIDGE_HOME"
}

# Prints problems with required env vars (names and shape only, never values).
env_problems() {
  local v val
  for v in "${REQUIRED_ENV[@]}"; do
    val="${!v:-}"
    if [[ -z "$val" ]]; then echo "$v is missing"; continue; fi
    case "$v" in
      SLACK_BOT_TOKEN) [[ "$val" == xoxb-* ]] || echo "$v does not start with xoxb- (use the Bot User OAuth Token)";;
      SLACK_APP_TOKEN) [[ "$val" == xapp-* ]] || echo "$v does not start with xapp- (use an App-Level Token with connections:write)";;
      GROK_WEBHOOK_URL) [[ "$val" == https://* ]] || echo "$v is not an https:// URL";;
    esac
  done
}

# Echo the bridge pid if it is running for this BRIDGE_HOME, else nothing.
running_pid() {
  local pid
  [[ -f "$PIDFILE" ]] || return 0
  pid="$(tr -dc '0-9' < "$PIDFILE")"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "bridge.py"; then
    echo "$pid"
  fi
}

stray_pids() {
  pgrep -f -- "bridge.py --home $BRIDGE_HOME\$" 2>/dev/null || true
}
