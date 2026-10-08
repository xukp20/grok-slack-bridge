#!/usr/bin/env bash
# Restart the bridge so it picks up new env values (tokens / webhook).
source "$(dirname "$(readlink -f "$0")")/lib.sh"
"$CODE_DIR/stop.sh"
exec "$CODE_DIR/start.sh" "$@"
