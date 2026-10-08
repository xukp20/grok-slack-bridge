# Configuration reference (`config.json`)

`config.json` in the install directory holds **non-secret** settings only
(`save_config` refuses token-like values). The bridge re-reads it for every
event, so most edits apply live; `outbox_*` and the four secrets need
`restart.sh`. Defaults: [`config.example.json`](../config.example.json).

Edit with `scripts/slackctl.sh config set <key> <value>` (lists accept
`U1,U2`; objects/lists of objects accept JSON) or by hand, then check with
`scripts/slackctl.sh access validate` and `scripts/doctor.sh`.

## Identity (filled automatically)

| Key | Meaning |
| --- | --- |
| `bot_name`, `agent_label` | label in payloads; free-form note of which agent receives events |
| `team_id`, `workspace`, `workspace_url`, `bot_user_id` | from `auth.test` at startup |
| `app_id` | from `bots.info` at startup (`auth.test` does not return it). Every event's `api_app_id` must match it, so keep it filled: if it is empty and `bots.info` fails, the bridge refuses everything |
| `owner_user_id` | the owner's member ID (`U…`), `scripts/set-owner.sh U…` |

## Access (one check for every entry point)

DMs, @mentions, followed-thread messages, plain channel messages, `/grok`,
buttons and the agent-view Stop button all go through the same check, in
this order:

1. the envelope's `team_id` and `api_app_id` must be this workspace and app;
2. unknown identities (no user/bot/app ID, humans from another workspace) are refused;
3. `channel_allowlist` (when non-empty) limits channels (DMs exempt);
4. `user_denylist` / `bot_denylist` (plus per-channel additions) refuse any
   matching user, bot or app ID — **denylists always win**;
5. humans: `human_access`; bots: `bot_access`.

| Key | Default | Meaning |
| --- | --- | --- |
| `human_access` | `owner_only` | `owner_only` \| `allowlist` (`user_allowlist`) \| `everyone`. The owner is always allowed |
| `user_allowlist` | `[]` | member IDs allowed when `human_access=allowlist` |
| `user_denylist` | `[]` | member IDs never served |
| `bot_access` | `none` | `none` \| `allowlist` (`bot_allowlist`) \| `all` |
| `bot_allowlist` | `[]` | objects, see below |
| `bot_denylist` | `[]` | user (`U…`), bot (`B…`) or app (`A…`) IDs never served; an app ID blocks every bot of that app |
| `channel_allowlist` | `[]` | if set, only these channels (DMs always allowed) |
| `channel_overrides` | `{}` | `{"C…": {…}}` per-channel settings, below |
| `deny_message` | … | fixed reply to refused **humans in DMs and `/grok`** (max once per hour per person). Refused channel messages and bots get nothing |

`bot_allowlist` entries match **real IDs only** (never display names). Name
at least one of `user_id`, `bot_id`, `app_id`; every ID you name must match.
Find them in a message's raw event (`user`, `bot_id`, `app_id`) or with
`slackctl.sh thread --include-refused`.

```json
{
  "label": "dot pilot",
  "user_id": "U0C7RCZK9GC", "bot_id": "B0C7GDAN9QV", "app_id": "A0C7GD92RLM",
  "channels": ["C0C79RD02AK"],
  "threads": ["C0C79RD02AK:1791460290.248329"],
  "expires_at": "2026-10-09T09:00:00+08:00",
  "max_turns": 3
}
```

- `channels` / `threads` (`"C…:ts"` or `"C…/ts"`) narrow where the bot is
  accepted; omit for anywhere.
- `expires_at`: ISO 8601 with a UTC offset, or epoch seconds. An
  unparseable value counts as already expired (fail closed).
- `max_turns` caps `max_bot_turns` for this bot.
- Bots can never use `/grok`, buttons, or text commands.

`channel_overrides["C…"]` may only make things **stricter**:
`human_access`, `bot_access` (a looser value is ignored and `doctor` warns),
`user_denylist` / `bot_denylist` (added to the global ones),
`user_allowlist` / `bot_allowlist` (lists of IDs that must *also* match),
`max_bot_turns` (lower wins), `bot_cooldown_seconds` (higher wins). `trigger`
is behaviour, not access, so a channel may pick any trigger.

Messages from refused senders are never forwarded and are hidden from
`slackctl.sh thread` (the agent's context) unless `--include-refused`.

Explain a decision without Slack: `slackctl.sh access check --user U… [--bot-id B… --app-id A…] --channel C… [--thread-ts T] --entry mention`.

Pre-2026-10 configs with `access` keep working (`access=everyone` is
honoured) until `slackctl.sh migrate-config`, which backs up `config.json`
and writes explicit keys with the deny-by-default values.

## Triggers and loop control

| Key | Default | Meaning |
| --- | --- | --- |
| `trigger` | `mention` | `mention`: DMs and @mentions only. `thread_follow`: also every later message in a thread where the bot was mentioned (no new mention needed). `all`: every message in channels the bot is in. DMs and `/grok` always count |
| `max_bot_turns` | `4` | forwarded **bot** messages per thread task; then bots are ignored until the owner says `new` |
| `bot_cooldown_seconds` | `10` | minimum gap between forwarded bot messages in one thread; extra messages are delayed (not dropped) |
| `command_words` | see example | words that act as commands when they are the whole message (mention allowed) |
| `stop_message`, `new_task_message`, `resume_message`, `help_text` | … | fixed replies |

Commands (humans only):

| Command | Who | Effect |
| --- | --- | --- |
| `stop` / `停` / `停止` / `别回了` | any allowed human, also without a mention inside a followed thread | thread → `stopped`; queued, in-flight and accepted operations → `stopped`; `reply.sh` refuses to post there (exit 3) |
| `new` / `新任务` | owner | new task in the thread: task id + 1, bot turn counter reset, state `active` |
| `resume` / `继续` | owner, only in a stopped/paused thread | state `active` (elsewhere `继续` is an ordinary message) |
| `help` / `帮助` | any allowed human, addressed to the bot | fixed help text |
| `status` / `状态` | owner, addressed to the bot | bridge + queue + thread summary |

The agent-view **Stop** button stops the thread the same way (after the
same access check). An @mention or DM from an allowed human continues a
stopped/paused thread without resetting the bot turn counter. After a
bridge restart, threads with bot activity are `paused`: bots stay ignored
until a human addresses the bot again. `[END]`, empty messages and similar
markers are never treated as control signals.

## Reliability

| Key | Default | Meaning |
| --- | --- | --- |
| `webhook_timeout_seconds` | `20` | a timeout **after** the request was sent is an unknown result: never resent automatically |
| `webhook_retries` | `3` | attempts when the request provably did not arrive (connect errors, 429, 502, 503); 1s/3s/9s backoff |
| `catchup_enabled`, `catchup_window_hours` | `true`, `24` | after a reconnect, read followed threads (`conversations.replies`) and process missed messages once |
| `retention_days` | `30` | finished receipts older than this are pruned |

State lives in `run/bridge.sqlite` (receipts, transitions, threads).
Operations move `received → queued → submitted → accepted → completed |
no_reply | stopped`, or `ignored`, `rejected`, `failed`, `unknown-result`,
`needs-reconciliation`. Inspect and resolve with `slackctl.sh ops list|show|resolve|retry`
and `slackctl.sh threads`.

## Outgoing messages and monitoring

| Key | Default | Meaning |
| --- | --- | --- |
| `mention_allowlist` | `[]` | extra user IDs replies may ping. Always allowed: the owner, the requester, `bot_allowlist` user IDs, `--allow-mention`. Other `<@U…>`, and every `<!channel>`/`<!here>`/`<!everyone>`/`<!subteam^…>`, are rendered inert |
| `outbox_max_items`, `outbox_min_interval_seconds` | `50`, `1.0` | bounded, rate-limited queue for bridge-originated messages (oldest dropped when full) |
| `report_channel`, `report_thread_ts` | `""` | where to report bridge (re)starts, repeated webhook problems and long disconnects. Empty = log only |
| `report_min_interval_seconds` | `900` | per report kind |
| `report_webhook_failures` | `3` | consecutive delivery problems before a report (`0` = never) |
| `report_disconnect_seconds` | `300` | report a Slack disconnect longer than this |
| `error_text` | … | fixed text sent to a human when their message could not be delivered (never exception details) |
| `error_reaction`, `react_on_receipt`, `ack_reaction` | `warning`, `true`, `eyes` | reactions |
| `slash_ack_text`, `slash_usage_text` | … | ephemeral answers to `/grok` |

## Presentation and privacy

| Key | Default | Meaning |
| --- | --- | --- |
| `agent_sessions`, `session_title_chars` | `true`, `60` | Slack agent view ("Working…" + Stop, one thread per conversation) when the app allows it |
| `dm_reply_in_thread` | `false` | thread replies in DMs too |
| `forward_raw_event` | `true` | include the raw Slack event in payloads |
| `log_message_text` | `false` | log message text (off for privacy) |

## Recipes

Owner only (the default):

```json
{"owner_user_id": "U0OWNER", "human_access": "owner_only", "bot_access": "none"}
```

A small team, one noisy person blocked, a read-only announcements channel:

```json
{
  "human_access": "allowlist",
  "user_allowlist": ["U0ALICE", "U0BOB"],
  "user_denylist": ["U0SPAM"],
  "channel_overrides": {"C0ANNOUNCE": {"human_access": "owner_only"}}
}
```

Short bot-to-bot pilot in one thread (expires, few turns, followed thread):

```json
{
  "bot_access": "allowlist",
  "bot_allowlist": [{"label": "dot pilot", "user_id": "U0C7RCZK9GC", "bot_id": "B0C7GDAN9QV",
                     "app_id": "A0C7GD92RLM", "threads": ["C0C79RD02AK:1791460290.248329"],
                     "expires_at": "2026-10-09T09:00:00+08:00", "max_turns": 3}],
  "max_bot_turns": 4,
  "bot_cooldown_seconds": 20
}
```
