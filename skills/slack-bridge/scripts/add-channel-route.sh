#!/usr/bin/env bash
# Route one Slack channel to a dedicated agent's webhook (or back to main).
#
#   add-channel-route.sh --channel C… --label "Name" \
#       --url-env GROK_WEBHOOK_URL_X --auth-env GROK_WEBHOOK_AUTH_X \
#       [--busy-policy interrupt_merge|queue] [--replace] [--allow-missing-env] [--dry-run] [--restart]
#   add-channel-route.sh --remove --channel C… [--dry-run]
#
# Backs up config.json, writes the route with `slackctl.sh route add|remove`
# (env var NAMES only: values that look like URLs/tokens are refused; the
# variables are checked for presence without printing them), then prints the
# next steps. It does NOT restart the bridge unless --restart is given.
# See docs/dedicated-channel-bot.md.
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install

action=add
restart=0
dry=0
args=()
for a in "$@"; do
  case "$a" in
    --restart) restart=1;;
    --remove) action=remove;;
    --dry-run) dry=1; args+=("$a");;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) args+=("$a");;
  esac
done

"$PY" "$CODE_DIR/slackctl.py" --home "$BRIDGE_HOME" route "$action" ${args[@]+"${args[@]}"}

if [[ $restart -eq 1 ]]; then
  if [[ $dry -eq 1 ]]; then
    echo "(dry run; not restarting)"
  else
    echo
    echo "restarting the bridge (--restart) ..."
    "$CODE_DIR/restart.sh"
  fi
fi
