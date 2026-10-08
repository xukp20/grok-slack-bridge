#!/usr/bin/env bash
# Validate install, env vars, Slack tokens, webhook reachability and the process.
#   doctor.sh [--ping-webhook] [--json]
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install
exec "$PY" "$CODE_DIR/slackctl.py" --home "$BRIDGE_HOME" doctor "$@"
