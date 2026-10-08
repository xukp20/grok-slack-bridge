#!/usr/bin/env bash
# Show whether the bridge is running, its heartbeat, and recent log lines.
# Exit 0 when running, 1 when stopped.
source "$(dirname "$(readlink -f "$0")")/lib.sh"

pid="$(running_pid)"
echo "home:      $BRIDGE_HOME"
if [[ -n "$pid" ]]; then
  echo "process:   running (pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' '))"
else
  echo "process:   stopped"
fi
if [[ -f "$RUN_DIR/heartbeat.json" && -n "$pid" ]]; then
  "${PY:-python3}" - "$RUN_DIR/heartbeat.json" <<'PY' 2>/dev/null || true
import json, sys, time
h = json.load(open(sys.argv[1]))
print(f"heartbeat: {int(time.time()) - h['heartbeat_at']}s ago, connected={h.get('connected')}")
print(f"events:    received={h.get('received')} forwarded={h.get('forwarded')} "
      f"skipped={h.get('skipped')} failed={h.get('failed')}")
print(f"bot:       {h.get('bot_user_id')}  webhook host: {h.get('webhook_host')}")
PY
fi
if [[ -f "$LOGFILE" ]]; then
  echo "--- last log lines ($LOGFILE)"
  tail -n "${1:-10}" "$LOGFILE"
fi
[[ -n "$pid" ]]
