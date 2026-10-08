#!/usr/bin/env bash
# Start the bridge in the background (idempotent). --foreground runs it attached.
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install

mapfile -t problems < <(env_problems)
if (( ${#problems[@]} )); then
  echo "slack-bridge: not starting; fix these environment variables first:" >&2
  printf '  - %s\n' "${problems[@]}" >&2
  echo "Secrets must be provided as environment variables (never written to files)." >&2
  exit 2
fi

if pid="$(running_pid)" && [[ -n "$pid" ]]; then
  echo "slack-bridge already running (pid $pid). Use restart.sh to pick up new settings."
  exit 0
fi
rm -f "$PIDFILE"

if [[ "${1:-}" == "--foreground" ]]; then
  exec "$PY" -u "$CODE_DIR/bridge.py" --home "$BRIDGE_HOME"
fi

mkdir -p "$RUN_DIR" "$LOG_DIR"
if [[ -f "$LOGFILE" ]] && (( $(stat -c %s "$LOGFILE") > 5*1024*1024 )); then
  mv -f "$LOGFILE" "$LOGFILE.1"
fi
offset=$( [[ -f "$LOGFILE" ]] && stat -c %s "$LOGFILE" || echo 0 )

# setsid + nohup: survive the launching shell/session ending.
setsid nohup "$PY" -u "$CODE_DIR/bridge.py" --home "$BRIDGE_HOME" >>"$LOGFILE" 2>&1 </dev/null &
launcher=$!

for _ in $(seq 1 40); do
  sleep 0.5
  new_log="$(tail -c +"$((offset + 1))" "$LOGFILE" 2>/dev/null || true)"
  if grep -q "socket mode connected" <<<"$new_log"; then
    echo "slack-bridge started (pid $(running_pid)); log: $LOGFILE"
    grep -E "authenticated as|socket mode connected" <<<"$new_log" | sed 's/^/  /'
    exit 0
  fi
  if ! kill -0 "$launcher" 2>/dev/null && [[ -z "$(running_pid)" ]]; then
    echo "slack-bridge failed to start. Log tail:" >&2
    tail -n 20 "$LOGFILE" >&2
    exit 1
  fi
done
echo "slack-bridge launched (pid $(running_pid)) but no 'connected' line after 20s; check $LOGFILE" >&2
exit 1
