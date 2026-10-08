#!/usr/bin/env bash
# Re-point the bridge after switching agent/account or rotating tokens.
#
#   reconfigure.sh [--ping-webhook] [--bot-name NAME] [--agent-label LABEL]
#                  [--owner U0123] [--no-restart]
#
# 1. Export the new values first (only the ones that changed):
#      GROK_WEBHOOK_URL / GROK_WEBHOOK_AUTH  -> new agent or account
#      SLACK_BOT_TOKEN / SLACK_APP_TOKEN     -> rotated tokens or a new box
# 2. Run this script. It updates non-secret config, runs doctor, and only if
#    every check passes restarts the bridge so the new env takes effect.
#    If doctor fails, the running bridge is left untouched.
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install

CTL=("$PY" "$CODE_DIR/slackctl.py" --home "$BRIDGE_HOME")
doctor_args=()
restart=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ping-webhook) doctor_args+=(--ping-webhook); shift;;
    --bot-name) "${CTL[@]}" config set bot_name "${2:?}"; shift 2;;
    --agent-label) "${CTL[@]}" config set agent_label "${2:?}"; shift 2;;
    --owner) "${CTL[@]}" set-owner "${2:?}"; shift 2;;
    --no-restart) restart=0; shift;;
    -h|--help) sed -n '2,15p' "$0"; exit 0;;
    *) die "unknown option $1";;
  esac
done

echo "==> doctor"
if ! "${CTL[@]}" doctor "${doctor_args[@]}"; then
  echo
  echo "Not restarting: fix the FAIL lines above, then re-run reconfigure.sh." >&2
  exit 1
fi
if (( restart )); then
  echo "==> restarting bridge"
  "$CODE_DIR/restart.sh"
else
  echo "==> --no-restart given; run restart.sh when ready"
fi
