#!/usr/bin/env bash
# Record the owner's Slack member ID (U…) in config.json.
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install
exec "$PY" "$CODE_DIR/slackctl.py" --home "$BRIDGE_HOME" set-owner "$@"
