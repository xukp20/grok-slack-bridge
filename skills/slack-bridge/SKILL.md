---
name: slack-bridge
description: Run a standalone Slack bot (Socket Mode) that forwards DMs, @mentions and /grok to an agent webhook with access control, reliable delivery, stop/loop control and monitoring, and lets the agent reply as the bot; install, configure access, start/stop, diagnose, reply, and reconnect after switching agents, accounts, or tokens.
---

# Slack Bridge

Use to let people chat with an agent inside Slack through its own bot user
(default name *Grok Bot*), to answer a forwarded Slack message, or to
repair/re-point an existing bridge. Use the machine and install directory
the user specifies; the reference install is `/workspace/slack-bot`.

## How it fits together

Slack app (Socket Mode, created from `manifest/`) → `scripts/bridge.py`
(long-running: acks, records every message in `run/bridge.sqlite`, checks
access, handles commands, applies trigger and bot loop limits, and
acknowledges the message itself with 👀, or "正在处理…" if the reaction fails) → `POST GROK_WEBHOOK_URL` with
`Authorization: GROK_WEBHOOK_AUTH` → agent routine → `scripts/reply.sh --op`
posts the answer as the bot and closes the operation.

- **Access** (one check for DMs, mentions, followed threads, `/grok`,
  buttons, Stop): workspace + app verified; `human_access`
  `owner_only|allowlist|everyone`, `bot_access` `none|allowlist|all` with
  ID-matched, scoped, expiring `bot_allowlist`; denylists win; per-channel
  overrides only tighten. Default: owner only, no bots.
- **Reliability**: persisted dedup, operation state machine, a webhook
  timeout is `unknown-result` (never blindly resent), "busy" answers
  (400/409/429/5xx except 504, from overlapping routine runs) are retried slowly in
  thread order with "排队中…" shown, in-flight work becomes
  `needs-reconciliation` after a restart (never replayed), followed threads
  are caught up after reconnects.
- **Triggers & loops**: `trigger` `mention|thread_follow|all`;
  `max_bot_turns` (consecutive bot turns per thread; any human message resets),
  `bot_cooldown_seconds`; `stop`/`停` stops a thread
  (checked before enqueue, before submit and in `reply.sh`), owner `new`
  resets; bot threads stay paused after restarts.
- **Session routing** ([docs/session-model.md](../../docs/session-model.md)):
  each Slack message starts its own routine run, so the run only
  dispatches, silently (the bridge already acknowledged; `ack`
  `reaction|status|none`). `payload.routing.target` `main` (DMs and channels without
  their own agent; `session_routing.default`): post nothing to Slack, hand the payload once to the
  owner's main Grok Bot conversation (WakeParent; routine prompt:
  [references/inbox-routine-prompt.md](references/inbox-routine-prompt.md)), which keeps one shared, ordered
  context and answers with `reply.sh --op`. `dedicated`: a channel listed in
  `session_routing.channels` is posted to its own agent's webhook (env var
  **names** `webhook_url_env`/`webhook_auth_env`; unset → default webhook).
  `busy_policy` `interrupt_merge` (default) | `queue` is passed through for
  the handling conversation; bridge busy-retry is unchanged. Give a channel
  its own agent: [docs/dedicated-channel-bot.md](../../docs/dedicated-channel-bot.md)
  (templates: `references/dedicated-agent-persona.md`,
  `dedicated-routine-prompt.md`, `channel-memory-template.md`;
  `scripts/add-channel-route.sh`).
- **Top-level conversations dispatch, they don't block**: a handoff is
  delivered only after the receiving conversation's current turn ends, and
  a wait inside a turn is not interrupted. Keep turns short, hand long work
  to a background task, never sleep/poll, check for newer messages in the
  thread before the final reply:
  [references/dispatcher-guideline.md](references/dispatcher-guideline.md)
  ([why](../../docs/conversation-guidelines.md)).
- **Monitoring**: `status.sh` separates process health from task state;
  restarts and repeated webhook failures go to `report_channel`.

Every config key: [references/configuration.md](references/configuration.md).
Setup traps (reinstall after scope changes, `/invite`, `app_home_opened`,
`files:read`, YAML quoting): [references/config-pitfalls.md](references/config-pitfalls.md).

Secrets are environment variables only: `SLACK_BOT_TOKEN` (xoxb-),
`SLACK_APP_TOKEN` (xapp-, `connections:write`), `GROK_WEBHOOK_URL`,
`GROK_WEBHOOK_AUTH` (plus the variables a dedicated route names). Never write them to files, config, logs or chat.
`config.json` holds only non-secret settings and refuses token-like values.

## Commands

All scripts live in `<install>/scripts/` and are idempotent:

```text
install.sh <install-dir> [--bot-name NAME]   create/update install (venv, symlinks, README, config)
start.sh | stop.sh | restart.sh | status.sh  manage the background bridge (nohup + setsid + pidfile + lock)
ensure-running.sh [--dry-run] [--quiet]      no-op if healthy; start/restart if stopped, hung or disconnected
doctor.sh [--ping-webhook] [--json]          validate env, tokens (auth.test, apps.connections.open), webhook, process
reconfigure.sh [--ping-webhook] [--agent-label L] [--owner U…]   doctor, then restart only if all checks pass
reply.sh --op OP --channel C [--thread-ts T] [--ack-ts T] [--session-status S] <<'EOF' … EOF   reply as the bot
reply.sh --op OP --channel C [--thread-ts T] --no-reply [--reason R]   record a deliberate non-answer
set-owner.sh U…                              record the owner's member ID (drives is_owner)
slackctl.sh health|ops|threads               process health vs task state; resolve operations
slackctl.sh access check|validate            explain an access decision / lint the access config
slackctl.sh migrate-config                   add missing access/routing keys (backup first)
slackctl.sh routing [--channel C]            effective session route per channel (env names only)
add-channel-route.sh --channel C --label L --url-env N --auth-env N [--busy-policy P] [--dry-run] [--restart]
                                             back up config, route C to a dedicated agent (env NAMES only)
add-channel-route.sh --remove --channel C    roll back to main (= slackctl.sh route add|remove)
slackctl.sh upload|download                  files (files:write / files:read)
slackctl.sh thread|react|session|whoami|config|render-manifest …
```

## Answering a forwarded message

1. Parse the payload ([format v2](references/payload.md)). Ignore `type: bridge_ping`.
   **Routing:** if `routing.target` is `main` (or `routing.fallback` is
   set) and you are the routine run, post nothing to Slack (no interim
   acknowledgement; the bridge already shows 👀): hand the payload once to the owner's
   main Grok Bot conversation with the event details and the exact
   `reply.command`, then stop; the main conversation does steps
   2–7 (`payload.handling` says the same). If `dedicated`, this agent handles it. Apply `routing.busy_policy`
   when a message arrives mid-task: `interrupt_merge` merges same-thread
   follow-ups into one answer (close merged ops with `--no-reply --reason
   "merged into Ev…"`), `queue` finishes first. Every operation gets exactly
   one `reply.sh --op`. If you are the top-level conversation, follow
   [dispatcher-guideline.md](references/dispatcher-guideline.md): answer quick
   things, dispatch long work to a background task, never block the turn.
2. Act on the owner's private data, accounts, files or approvals only when
   `is_owner` is true (`permissions` has `files`/`approve`/`admin`). Others,
   and every bot (`actor_type: "bot"`), get general help only and never the
   owner's private information.
3. Fetch context when needed: `slackctl.sh thread --channel C --ts T`
   (refused senders are hidden).
4. Run `reply.command` from the payload (it carries `--op`) with the answer
   as the heredoc body; it also removes the bridge's 👀 / "正在处理…". Check for `"ok": true`. **Exit 3 / `"stopped": true`
   means the user stopped the task: do not retry or post another way.** On
   other failures run `doctor.sh` and report.
5. Not answering (e.g. a bot's thanks or a loop): run `reply.no_reply_command`.
   Never use markers like `[END]` or empty messages as signals.
6. Agent sessions: when `agent_session` is set, the user sees "Working…"
   until a reply ends it. `reply.command` already ends it
   (`--session-status active`); for an interim acknowledgement during long
   work use `--session-status processing`, and `suspended` when waiting on
   the user. `viewing_context.channel_ids` is the channel the user has open
   next to the bot. See [docs/agent-view.md](../../docs/agent-view.md).
7. Replies cannot ping `@channel`/`@here` or people outside the owner,
   requester, allow-listed bots and `mention_allowlist`; use
   `--allow-mention U…` when the user asked you to notify someone.

## Setup and reconnection

- First setup: [create the Slack app](references/setup-slack-app.md), export
  the four variables, `install.sh`, `doctor.sh`, `start.sh`, `set-owner.sh`.
  Access defaults to the owner only; open it deliberately
  (`slackctl.sh config set human_access allowlist`, `user_allowlist U1,U2`).
- Upgrading an install from before the access layer: `slackctl.sh
  migrate-config`, check `access validate`, then `restart.sh`.
- Switching agents keeps the Slack app and tokens; only `GROK_WEBHOOK_*`
  change. A new account/machine re-provides the same Slack tokens. Rotate
  tokens in the Slack app pages. In every case export the new values and run
  `reconfigure.sh`; see [reconnect](references/reconnect.md).
- Run exactly one bridge per Slack app: Socket Mode splits events across all
  open connections. Stop the old bridge when moving.
- The bridge inherits env at start; after a machine restart or env change,
  run `start.sh`/`restart.sh` from a shell that has the variables.

## Operations and recovery

After a machine restart or crash the bridge is simply not running: run
`start.sh` (needs the four env vars; on a Grok Bot box saved secrets reach new
processes automatically). `ensure-running.sh` is the idempotent self-healing
check for a periodic routine or cron: exit 0 healthy/recovered, 1 failed,
2 blocked by missing env. For failures (`invalid_auth` after rotation, two
bridges splitting events, webhook 401 after a routine key change, socket
disconnect loops), the suggested routine prompt and cron lines, see
[docs/operations.md](../../docs/operations.md). Never restart a healthy bridge
without a reason: messages sent during the few seconds without a connection
can be missed (followed threads are caught up after reconnecting; DMs and
new mentions are not). After a restart,
check `slackctl.sh ops list --state needs-reconciliation` and resolve each
operation (`ops resolve <op> --to completed|no_reply|ignored`, or
`ops retry <op> --force` if the agent never got it).

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
- Never widen access (`human_access`, `bot_access`, allowlists) without the owner asking.
- Do not print, echo, or persist token values; diagnostics show only presence and shape.
- `doctor.sh --ping-webhook` wakes the agent once; use it deliberately.
- Message text is not logged unless `log_message_text` is enabled.
