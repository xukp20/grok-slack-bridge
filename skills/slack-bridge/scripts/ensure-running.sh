#!/usr/bin/env bash
# Idempotent self-healing check for a periodic routine or cron.
#
#   ensure-running.sh [--dry-run] [--stale-seconds N] [--quiet]
#
# - Bridge running, heartbeat fresh, socket connected  -> do nothing (exit 0)
# - Bridge not running                                  -> start.sh
# - Heartbeat older than N s (hung) or socket disconnected
#   for longer than N s                                 -> restart.sh
# Default N = 300 (the bridge writes a heartbeat every 30 s).
# Starting needs the 4 env vars; if they are missing it reports that and
# exits 2 without touching anything. One line per run is appended to
# logs/ensure-running.log.
#
# Exit codes: 0 healthy or recovered, 1 recovery failed, 2 cannot start
# (env/install problem), 3 another ensure-running is in progress.
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install

dry_run=0; quiet=0; stale="${SLACK_BRIDGE_STALE_SECONDS:-300}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1; shift;;
    --quiet) quiet=1; shift;;
    --stale-seconds) stale="${2:?}"; shift 2;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) die "unknown option $1";;
  esac
done

mkdir -p "$RUN_DIR" "$LOG_DIR"
exec 9>"$RUN_DIR/ensure-running.lock"
if ! flock -n 9; then
  echo "ensure-running: another check is in progress"; exit 3
fi

ENSURE_LOG="$LOG_DIR/ensure-running.log"
report() {  # report <action> <detail>
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S %z') action=$1 $2"
  echo "$line" >> "$ENSURE_LOG"
  if (( ! quiet )) || [[ "$1" != none ]]; then echo "ensure-running: $line"; fi
}

pid="$(running_pid)"
reason=""
if [[ -n "$pid" ]]; then
  # Health from heartbeat.json: age, connected flag, time since last connected.
  read -r hb_age connected disc_for < <("$PY" - "$RUN_DIR/heartbeat.json" <<'PY' 2>/dev/null || echo "-1 unknown -1"
import json, sys, time
try:
    h = json.load(open(sys.argv[1]))
except Exception:
    print("-1 unknown -1"); sys.exit()
now = int(time.time())
last = h.get("last_connected_at")
disc = 0 if h.get("connected") else (now - int(last) if last else -1)
print(now - int(h.get("heartbeat_at", 0)), str(bool(h.get("connected"))).lower(), disc)
PY
)
  uptime="$(ps -o etimes= -p "$pid" | tr -d ' ')"
  if [[ "$hb_age" == -1 ]]; then
    # No heartbeat yet: fine right after start, otherwise suspicious.
    (( uptime > stale )) && reason="no heartbeat after ${uptime}s"
  elif (( hb_age > stale )); then
    reason="heartbeat stale (${hb_age}s old)"
  elif [[ "$connected" == false ]] && (( disc_for > stale )); then
    reason="socket disconnected for ${disc_for}s"
  fi
  if [[ -z "$reason" ]]; then
    report none "healthy pid=$pid heartbeat=${hb_age}s connected=$connected"
    exit 0
  fi
fi

mapfile -t problems < <(env_problems)
if (( ${#problems[@]} )); then
  state="not running"; [[ -n "$pid" ]] && state="unhealthy pid=$pid ($reason)"
  report blocked "bridge $state; cannot (re)start: ${problems[*]}"
  exit 2
fi

if [[ -n "$pid" ]]; then
  if (( dry_run )); then report would-restart "pid=$pid $reason"; exit 0; fi
  if "$CODE_DIR/restart.sh" >>"$ENSURE_LOG" 2>&1; then
    report restarted "was pid=$pid ($reason); now pid=$(running_pid)"; exit 0
  fi
  report failed "restart after: $reason; see $LOGFILE"; exit 1
fi

if (( dry_run )); then report would-start "bridge not running"; exit 0; fi
if "$CODE_DIR/start.sh" >>"$ENSURE_LOG" 2>&1; then
  report started "pid=$(running_pid)"; exit 0
fi
report failed "start failed; see $LOGFILE"; exit 1
