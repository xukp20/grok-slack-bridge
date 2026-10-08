#!/usr/bin/env bash
# Show bridge process health and task state (reported separately), then
# recent log lines. Exit 0 when the process is running, 1 when stopped
# (task problems such as needs-reconciliation do not change the exit code).
source "$(dirname "$(readlink -f "$0")")/lib.sh"

pid="$(running_pid)"
echo "home:      $BRIDGE_HOME"
if [[ -n "$pid" ]]; then
  echo "process:   running (pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' '))"
else
  echo "process:   stopped"
fi
if [[ -x "$PY" ]]; then
  "$PY" "$CODE_DIR/slackctl.py" --home "$BRIDGE_HOME" health || true
fi
if [[ -f "$LOGFILE" ]]; then
  echo "--- last log lines ($LOGFILE)"
  tail -n "${1:-10}" "$LOGFILE"
fi
[[ -n "$pid" ]]
