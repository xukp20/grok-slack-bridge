#!/usr/bin/env bash
# Post a message as the bot.
#   reply.sh --channel C123 [--thread-ts 1712.0001] [--ack-ts 1712.0001] [--text "hi" | --text-file f | <stdin]
# Markdown is rendered with a Slack markdown block (falls back to mrkdwn).
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install
exec "$PY" "$CODE_DIR/slackctl.py" --home "$BRIDGE_HOME" reply "$@"
