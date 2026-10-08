#!/usr/bin/env bash
# Run slackctl.py with the install's virtualenv. See: slackctl.sh --help
source "$(dirname "$(readlink -f "$0")")/lib.sh"
require_install
exec "$PY" "$CODE_DIR/slackctl.py" --home "$BRIDGE_HOME" "$@"
