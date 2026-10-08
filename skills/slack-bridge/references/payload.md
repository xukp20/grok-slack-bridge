# Webhook payload (version 2)

`POST GROK_WEBHOOK_URL` with `Content-Type: application/json` and
`Authorization: <GROK_WEBHOOK_AUTH>`; one request per forwarded operation.
Channels routed to a dedicated agent (`session_routing`) are posted to that
agent's webhook instead (the URL/Authorization in the env variables the
route names); everything else uses the default webhook.
A request is sent at most once unless it provably never arrived (see
Delivery), so the agent should not expect duplicates; it can still use
`operation_id` to recognise one.

```json
{
  "source": "slack-bridge", "version": 2, "type": "message",
  "bot_name": "Grok Bot", "bot_user_id": "U0…", "team_id": "T0…", "workspace": "…",
  "operation_id": "Ev0…", "event_id": "Ev0…", "event_type": "message",
  "entry": "mention", "catchup": false,
  "conversation": "channel", "channel": "C0…",
  "user": "U0…", "user_name": "…", "user_real_name": "…",
  "actor_type": "bot", "bot": {"user_id": "U0…", "bot_id": "B0…", "app_id": "A0…"},
  "is_owner": false, "owner_configured": true, "permissions": ["reply"],
  "text": "hi", "ts": "1791460300.000100", "thread_ts": "1791460290.248329", "files": [],
  "thread": {"thread_key": "T0…:C0…:1791460290.248329:A0…", "task_id": 1,
             "state": "active", "bot_turns": 1},
  "routing": {"target": "main", "busy_policy": "interrupt_merge", "source": "default",
              "label": "", "webhook": "default"},
  "reply": {
    "channel": "C0…", "thread_ts": "1791460290.248329",
    "command": "/workspace/slack-bot/scripts/reply.sh --op Ev0… --channel C0… --thread-ts 1791460290.248329 --ack-ts 1791460300.000100 <<'EOF'\n<your reply>\nEOF",
    "no_reply_command": "/workspace/slack-bot/scripts/reply.sh --op Ev0… --channel C0… --thread-ts 1791460290.248329 --no-reply --ack-ts 1791460300.000100",
    "readme": "/workspace/slack-bot/README.md"
  },
  "acknowledged": {"by": "bridge", "mode": "reaction", "emoji": "eyes"},
  "handling": {"routine_run": "silent_handoff", "post_to_slack": "never",
               "instructions": "Main route: post NOTHING to Slack from the routine run …"},
  "agent_session": null, "viewing_context": null, "raw_event": {"…": "…"}
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `source`, `version` | `"slack-bridge"`, `2` | version 2 added `operation_id`, `entry`, `actor_type`, `bot`, `permissions`, `thread`, `reply.no_reply_command`, `--op` |
| `type` | `"message"` \| `"bridge_ping"` | `bridge_ping` only from `doctor --ping-webhook`: do nothing |
| `operation_id` | string | the bridge's id for this operation (Slack `event_id`, `catchup:C:ts` for catch-up, `cmd:<trigger>` for `/grok`). Pass it back with `--op` |
| `event_id`, `event_type` | | Slack envelope; `event_type` is `message`, `app_mention` or `slash_command` |
| `entry` | string | how the message reached the bot: `dm`, `mention`, `thread_follow` (later message in a followed thread), `channel` (`trigger=all`), `slash_command` |
| `catchup` | bool | `true` when found by reading a followed thread after a reconnect |
| `conversation`, `channel` | | `dm`, or the channel type (`channel`, `group`, `mpim`) |
| `user`, `user_name`, `user_real_name` | string | sender (for bots: the bot's user) |
| `actor_type` | `"human"` \| `"bot"` | bots only arrive when `bot_access` allows them |
| `bot` | object \| absent | `{user_id, bot_id, app_id}` for bot senders |
| `is_owner` | bool | `true` only for the human owner; always `false` for bots |
| `permissions` | list | `["reply","files","approve","admin"]` for the owner, `["reply"]` for everyone else. Only the owner may make the agent act on private data, accounts, files or approvals |
| `owner_configured` | bool | `owner_user_id` is set |
| `text` | string | Slack-formatted text with the bot mention removed |
| `ts`, `thread_ts` | string \| null | `ts` is empty for `/grok` |
| `files` | list | id, name, mimetype, size, permalink (no contents; `slackctl.sh download --file-id`) |
| `thread` | object | task state of the thread: `thread_key` (team:channel:root:app), `task_id`, `state` (`active`, `completed`, `no_reply`, `stopped`, `paused`), `bot_turns` |
| `routing` | object | who handles the message (`docs/session-model.md`): `target` `main` (hand the payload to the owner's main Grok Bot conversation, which answers with `reply.command`) or `dedicated` (this channel's own agent handles it); `busy_policy` `interrupt_merge` \| `queue` (what the handling conversation does with a message arriving while it works); `source` `default` \| `channel`; `label`; `webhook` `default` \| `dedicated` (which webhook the bridge used); for dedicated routes `webhook_url_env`/`webhook_auth_env` (env var **names**, never values); `fallback` (reason) when a dedicated route was unusable and the default webhook was used |
| `acknowledged` | object | `{by: "bridge", mode, emoji}`: the bridge acknowledges the message itself right after queueing it (`ack` config: 👀 reaction by default, assistant status "正在处理…" if the reaction fails, in DMs/agent threads). The routine never needs to post a "received" message. `--ack-ts` in the reply commands lets `reply.sh --op` remove whatever the bridge added |
| `handling` | object | what the receiving routine run should do: `routine_run` `silent_handoff` (main route: post nothing, hand off once with `reply.command`, end) or `answer` (dedicated route: answer here, one final reply); `post_to_slack` `never` \| `final_reply_only`; `instructions` (plain text) |
| `reply.channel`, `reply.thread_ts` | | where to answer (`/grok` answers go to the user's DM) |
| `reply.command` | string | ready-to-run `reply.sh --op …` with a heredoc placeholder |
| `reply.no_reply_command` | string | records a deliberate non-answer |
| `reply.readme` | string | path of the runtime README |
| `agent_session` | object \| null | `{channel, thread_ts, status}` when the thread is an agent session ("Working…"); `reply.command` then ends with `--session-status active` |
| `viewing_context` | object \| null | `{channel_ids, updated_at}` from `app_context_changed` |
| `raw_event` | object | the untouched Slack event (`forward_raw_event`) |

## What is forwarded

Every event is checked in this order; anything that fails is recorded and
**not** forwarded:

1. noise is dropped without a receipt: edits, deletions, joins, hidden
   events, the bot's own messages (by user, bot or app ID);
2. duplicates (same `event_id`, or same `channel:ts` from another event
   type) are dropped; the same id with different content is rejected;
3. access (`team_id`, `api_app_id`, denylists, `human_access`,
   `bot_access`, channel overrides);
4. text commands (`stop`, `new`, `resume`, `help`, `status`) are handled by
   the bridge;
5. thread state: `stopped`/`paused` threads take no bot messages, and humans
   must address the bot to continue them;
6. trigger (`mention` / `thread_follow` / `all`); DMs always count;
7. bot loop limits (`max_bot_turns`, `bot_cooldown_seconds`).

Agent events (`app_context_changed`, `agent_session_stopped`,
`agent_session_title_changed`, `app_home_opened`) are handled inside the
bridge.

## Delivery

| Webhook result | Operation state | Resent? |
| --- | --- | --- |
| 2xx | `accepted` | — |
| connect/DNS/TLS failure (request never sent) | `queued`, 1s/3s/9s backoff, then `failed` | yes, up to `webhook_retries` |
| 400, 408, 409, 425, 429, 5xx except 504 ("busy": usually the routine is still running the previous message, since every Slack message starts its own run) | `queued`; retried after 20s, 40s, 80s, 160s, then every 5 min, up to 15 min (`webhook_busy_*`), in thread order; "排队中…" / ⏳ meanwhile | yes; when exhausted: `failed` + `busy_failed_text` asking the user to resend |
| timeout or disconnect after sending, HTTP 504 (a gateway timed out after forwarding; the run may have started) | `unknown-result` | **no**: the agent may have it; resolve with `slackctl.sh ops resolve` or `ops retry --force` |
| 401/403 (stale Authorization), 404/410 (stale URL), other 4xx | `failed` | no; `error_reaction` + fixed `error_text` to the requester |

Routing does not change delivery: busy-retry, per-thread order and the
no-blind-resend rules apply to dedicated webhooks exactly as above, and a
retried operation keeps the webhook chosen when it was queued.

After a restart, operations that were `submitted` or `accepted` become
`needs-reconciliation` and are never replayed; `reply.sh --op` still
completes them. Receipts left undecided by a crash are decided once.
