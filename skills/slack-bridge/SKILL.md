---
name: slack-bridge
description: Run a standalone Slack bot (Socket Mode) that forwards DMs and @mentions to an agent webhook and lets the agent reply as the bot; install, start/stop, diagnose, reply, and reconnect after switching agents, accounts, or tokens.
---

# Slack Bridge

Use to let people chat with an agent inside Slack through its own bot user
(default name *Grok Bot*), to answer a forwarded Slack message, or to
repair/re-point an existing bridge. Use the machine and install directory
the user specifies; the reference install is `/workspace/slack-bot`.

## How it fits together

Slack app (Socket Mode, created from `manifest/`) → `scripts/bridge.py`
(long-running, acks, filters, dedupes, 👀) → `POST GROK_WEBHOOK_URL` with
`Authorization: GROK_WEBHOOK_AUTH` → agent routine → `scripts/reply.sh`
posts the answer as the bot.

Secrets are environment variables only: `SLACK_BOT_TOKEN` (xoxb-),
`SLACK_APP_TOKEN` (xapp-, `connections:write`), `GROK_WEBHOOK_URL`,
`GROK_WEBHOOK_AUTH`. Never write them to files, config, logs or chat.
`config.json` holds only non-secret settings and refuses token-like values.

## Commands

All scripts live in `<install>/scripts/` and are idempotent:

```text
install.sh <install-dir> [--bot-name NAME]   create/update install (venv, symlinks, README, config)
start.sh | stop.sh | restart.sh | status.sh  manage the background bridge (nohup + setsid + pidfile)
doctor.sh [--ping-webhook] [--json]          validate env, tokens (auth.test, apps.connections.open), webhook, process
reconfigure.sh [--ping-webhook] [--agent-label L] [--owner U…]   doctor, then restart only if all checks pass
reply.sh --channel C [--thread-ts T] [--ack-ts T] <<'EOF' … EOF    reply as the bot (Markdown)
set-owner.sh U…                              record the owner's member ID (drives is_owner)
slackctl.sh thread|react|whoami|config|render-manifest …
```

## Answering a forwarded message

1. Parse the payload ([format](references/payload.md)). Ignore `type: bridge_ping`.
2. Act on the owner's private data or accounts only when `is_owner` is true;
   others get general help and never the owner's private information.
3. Fetch context when needed: `slackctl.sh thread --channel C --ts T`.
4. Run `reply.command` from the payload with the answer as the heredoc body.
   Check for `"ok": true`; on failure run `doctor.sh` and report.

## Setup and reconnection

- First setup: [create the Slack app](references/setup-slack-app.md), export
  the four variables, `install.sh`, `doctor.sh`, `start.sh`, `set-owner.sh`.
- Switching agents keeps the Slack app and tokens; only `GROK_WEBHOOK_*`
  change. A new account/machine re-provides the same Slack tokens. Rotate
  tokens in the Slack app pages. In every case export the new values and run
  `reconfigure.sh`; see [reconnect](references/reconnect.md).
- Run exactly one bridge per Slack app: Socket Mode splits events across all
  open connections. Stop the old bridge when moving.
- The bridge inherits env at start; after a machine restart or env change,
  run `start.sh`/`restart.sh` from a shell that has the variables.

## Alternatives

Other Slack routes and when to prefer them are in
[docs/slack-connection-options.md](../../docs/slack-connection-options.md):
the agent's Slack MCP connector (read/search/draft as the user, no inbound
chat), Composio's Slack toolkit (separate OAuth, outbound only), and the
Cursor Slack app with a Slack-listener routine on a channel such as
`#ask-bot` (inbound, but replies appear as the Cursor app, no DMs). This
standalone app was chosen for its own bot identity, DMs, real-time delivery
without a public URL, and easy re-pointing. Publishing uses
`gh auth login --web` (device login); see [docs/publishing.md](../../docs/publishing.md).

## Boundaries

- Do not post to Slack except replies to forwarded messages or what the user asked for.
- Do not print, echo, or persist token values; diagnostics show only presence and shape.
- `doctor.sh --ping-webhook` wakes the agent once; use it deliberately.
- Message text is not logged unless `log_message_text` is enabled.
