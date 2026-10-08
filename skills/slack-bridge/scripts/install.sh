#!/usr/bin/env bash
# Create or update a live install of the Slack bridge.
#
#   install.sh [INSTALL_DIR] [--bot-name "Grok Bot"]
#
# INSTALL_DIR (default: $SLACK_BRIDGE_HOME or ~/slack-bot) gets:
#   scripts/, manifest/, references/, SKILL.md -> symlinks into this skill
#   README.md   generated runtime guide (paths filled in)
#   config.json non-secret settings (created once, never overwritten)
#   .venv/      Python virtualenv with slack_sdk
#   logs/, run/ log file, pidfile, heartbeat
# Safe to re-run: it refreshes symlinks, README and dependencies only.
set -euo pipefail

SRC_SCRIPTS="$(cd "$(dirname "$(readlink -f "$0")")" && pwd -P)"
SKILL_DIR="$(cd "$SRC_SCRIPTS/.." && pwd -P)"

TARGET=""
BOT_NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bot-name) BOT_NAME="${2:?--bot-name needs a value}"; shift 2;;
    -h|--help) sed -n '2,14p' "$0"; exit 0;;
    -*) echo "unknown option $1" >&2; exit 2;;
    *) TARGET="$1"; shift;;
  esac
done
TARGET="${TARGET:-${SLACK_BRIDGE_HOME:-$HOME/slack-bot}}"
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd -P)"

PYTHON="${PYTHON:-python3}"
"$PYTHON" - <<'PY' || { echo "Python 3.10+ is required" >&2; exit 2; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

echo "==> installing slack-bridge into $TARGET (code: $SKILL_DIR)"
mkdir -p "$TARGET/logs" "$TARGET/run"
chmod 700 "$TARGET/logs" "$TARGET/run"

if [[ "$TARGET" != "$SKILL_DIR" ]]; then
  for item in scripts manifest references SKILL.md; do
    dest="$TARGET/$item"
    if [[ -e "$dest" && ! -L "$dest" ]]; then
      echo "refusing to replace non-symlink $dest" >&2; exit 2
    fi
    ln -sfn "$SKILL_DIR/$item" "$dest"
  done
fi

sed "s#@HOME@#$TARGET#g" "$SKILL_DIR/references/runtime-readme.md" > "$TARGET/README.md.tmp"
mv "$TARGET/README.md.tmp" "$TARGET/README.md"

if [[ ! -x "$TARGET/.venv/bin/python" ]]; then
  echo "==> creating virtualenv"
  "$PYTHON" -m venv "$TARGET/.venv"
fi
echo "==> installing Python dependencies"
"$TARGET/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$SKILL_DIR/requirements.txt"

CTL=("$TARGET/.venv/bin/python" "$SKILL_DIR/scripts/slackctl.py" --home "$TARGET")
if [[ ! -f "$TARGET/config.json" ]]; then
  "${CTL[@]}" config set bot_name "${BOT_NAME:-Grok Bot}" >/dev/null
  echo "==> created $TARGET/config.json"
elif [[ -n "$BOT_NAME" ]]; then
  "${CTL[@]}" config set bot_name "$BOT_NAME"
fi

cat <<MSG

Installed. Next steps:
  1. Create the Slack app from $TARGET/manifest/slack-app-manifest.yaml
     (or: $TARGET/scripts/slackctl.sh render-manifest --name "My Bot")
  2. Export SLACK_BOT_TOKEN, SLACK_APP_TOKEN, GROK_WEBHOOK_URL, GROK_WEBHOOK_AUTH
  3. $TARGET/scripts/doctor.sh
  4. $TARGET/scripts/start.sh
  5. $TARGET/scripts/set-owner.sh U0123456789   (your Slack member ID)
MSG
