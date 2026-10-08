#!/usr/bin/env bash
# Stop the bridge (idempotent).
source "$(dirname "$(readlink -f "$0")")/lib.sh"

pids="$(running_pid) $(stray_pids)"
pids="$(tr ' ' '\n' <<<"$pids" | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ' || true)"
if [[ -z "${pids// }" ]]; then
  rm -f "$PIDFILE"
  echo "slack-bridge is not running"
  exit 0
fi
# shellcheck disable=SC2086
kill -TERM $pids 2>/dev/null || true
for _ in $(seq 1 20); do
  alive=""
  for p in $pids; do kill -0 "$p" 2>/dev/null && alive+="$p "; done
  [[ -z "$alive" ]] && break
  sleep 0.5
done
if [[ -n "${alive:-}" ]]; then
  # shellcheck disable=SC2086
  kill -KILL $alive 2>/dev/null || true
fi
rm -f "$PIDFILE"
echo "slack-bridge stopped (pid $pids)"
