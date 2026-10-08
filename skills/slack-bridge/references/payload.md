# Webhook payload (version 1)

`POST GROK_WEBHOOK_URL` with headers `Content-Type: application/json` and
`Authorization: <GROK_WEBHOOK_AUTH>`; one request per accepted Slack event.

| Field | Type | Notes |
| --- | --- | --- |
| `source` | `"slack-bridge"` | constant |
| `version` | `1` | bumped on breaking changes |
| `type` | `"bridge_ping"` | only on `doctor --ping-webhook` test requests |
| `bot_name`, `bot_user_id`, `workspace`, `team_id` | string | from config / `auth.test` |
| `event_id`, `event_time`, `event_type` | | Slack envelope; `event_type` is `message` (DM) or `app_mention` |
| `conversation` | `"dm"` \| `"channel"` | |
| `channel`, `user`, `user_name`, `user_real_name` | string | sender info |
| `is_owner`, `owner_configured` | bool | `user == config.owner_user_id` |
| `text` | string | Slack-formatted text with the bot mention removed |
| `ts`, `thread_ts` | string \| null | |
| `files` | list | id, name, mimetype, size, permalink (no file contents) |
| `reply.channel`, `reply.thread_ts` | | where to answer |
| `reply.command` | string | ready-to-run `reply.sh` invocation with a heredoc placeholder |
| `reply.readme` | string | path of the runtime README |
| `raw_event` | object | the untouched Slack event (`forward_raw_event`) |

Filtering before forwarding: only `app_mention` and DM `message` events;
messages from bots or the bot itself, edits, deletions, joins and other
subtypes (except `file_share`/`thread_broadcast`) are dropped; retries are
deduplicated by `event_id` and `channel:ts`. With `access: owner_only`,
non-owner messages are not forwarded.

Delivery: up to `webhook_retries` attempts with 1s/3s/9s backoff on network
errors, 429 and 5xx. 401/403/404/410 are treated as a stale webhook (no
retry) and logged with a hint to run `reconfigure.sh`; the message gets the
`error_reaction`.
