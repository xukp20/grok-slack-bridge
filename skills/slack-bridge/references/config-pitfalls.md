# Configuration pitfalls

Problems we actually hit while setting up and running the bridge, with the
symptom you see and the fix. Run `scripts/doctor.sh` first; most of these
show up there.

## Slack app

| Symptom | Cause | Fix |
| --- | --- | --- |
| The agent entry point / split view never appears, even with the agent view enabled | `app_home_opened` is not subscribed (or the Messages tab is off) | keep `app_home_opened` in `bot_events` and `messages_tab_enabled: true`; reinstall |
| New scope or event "does nothing"; API calls return `missing_scope` | manifest changed but the app was not reinstalled; the token keeps its old scopes | after every scope/event change: **Install App → Reinstall to workspace**. If Slack shows a new bot token, export it and `restart.sh` |
| @mention in a channel gets no answer; `chat.postMessage` → `not_in_channel` | the bot is not a member | `/invite @Grok Bot` in the channel (private channels too). `message.channels` events and thread catch-up only cover channels the bot is in |
| `thread_follow` never triggers | missing `message.channels`/`message.groups` events or `channels:history`/`groups:history` | keep them in the manifest; reinstall |
| Downloading a file "succeeds" but the file is an HTML page | `files:read` missing (Slack answers `url_private` with its login page, HTTP 200) | add `files:read`, reinstall; `slackctl.sh download` detects this and says so |
| `/grok` → "dispatch_failed" or nothing | `commands` scope missing, the command is not in the manifest, or the bridge is not running | keep `slash_commands` + `commands`; reinstall; `status.sh` |
| `/grok` works but no DM answer | `im:write` missing (`conversations.open`) | add it; reinstall |
| Buttons do nothing | `interactivity.is_enabled: false` | set `true` (Socket Mode needs no request URL); reinstall |
| Half the messages never arrive | two bridges (or another Socket Mode client) use the same app token: Slack spreads events across connections | run exactly one bridge per app; `run/bridge.lock` prevents a second one on the same machine, not on another machine |
| `api_app_id` mismatch / everything refused after moving | config `app_id` belongs to a different app | the bridge refreshes `app_id` from `bots.info` at startup; check `doctor.sh` |
| `auth.test` works but `app_id` stays empty | `auth.test` does not return the app id | the bridge uses `bots.info`; keep `users:read` and let it fill `app_id` once |
| External (Slack Connect) people are refused | their `team` differs from the workspace | by design; they are unknown identities |

## Manifest YAML

- `#` starts a comment: quote colours (`"#111827"`).
- `[` and `{` start lists/objects: quote `"[question or task]"`.
- `: ` inside a value needs quotes (`"Summary: today"`); so do values
  starting with `*`, `&`, `!`, `|`, `>`, `%`, `@`, `` ` ``.
- Indent with spaces, two per level; list items under one key line up.
- `true`/`false` unquoted for booleans; quote strings like `"on"`/`"yes"`.
- Socket Mode apps have **no** `request_url` for events, slash commands or
  interactivity.
- Generate rather than hand-edit: `slackctl.sh render-manifest --name "My Bot"`
  (add `--format json` for JSON, `--out FILE` to write a file) quotes
  everything correctly. The annotated layout with a reason
  for every line is [`manifest/slack-app-manifest.annotated.yaml`](../manifest/slack-app-manifest.annotated.yaml).

## config.json

- JSON has no comments. Unknown keys are kept and ignored, so a `"_note"`
  key is fine for notes; do not use `//`.
- Never put tokens in it: `config set` refuses `xoxb-`, `xapp-`, `Bearer …`
  values. Secrets are environment variables only.
- Edits apply live (the bridge re-reads it per event); `outbox_*` and env
  changes need `restart.sh`.
- Lists from the command line: `slackctl.sh config set user_denylist U1,U2`.
  Objects: `slackctl.sh config set channel_overrides '{"C0…": {"trigger": "thread_follow"}}'`.
- `bot_allowlist` matches real IDs only. A bot message carries `user`
  (`U…`), `bot_id` (`B…`) and `app_id` (`A…`), often without a subtype; copy
  all three from its raw event. Display names are never used.
- `expires_at` needs a UTC offset (`2026-10-09T09:00:00+08:00`) or epoch
  seconds; a value that does not parse counts as expired.
- `channel_overrides` can only tighten access: a looser `human_access` /
  `bot_access` there is ignored (doctor warns).
- Old configs with `access`: run `slackctl.sh migrate-config` (backs up the
  file; `access: everyone` becomes `human_access: owner_only` — re-open
  explicitly if you really want everyone).
- `report_channel` is empty by default (log only). Set it, and the bot must
  be a member of that channel.

## Example config.json

```json
{
  "_note": "Grok Bot live install; secrets are env vars only",
  "bot_name": "Grok Bot",
  "owner_user_id": "U0OWNER",

  "human_access": "owner_only",
  "user_allowlist": [],
  "user_denylist": [],
  "bot_access": "allowlist",
  "bot_allowlist": [
    {"label": "dot pilot", "user_id": "U0C7RCZK9GC", "bot_id": "B0C7GDAN9QV", "app_id": "A0C7GD92RLM",
     "threads": ["C0C79RD02AK:1791460290.248329"], "expires_at": "2026-10-09T09:00:00+08:00",
     "max_turns": 3}
  ],
  "bot_denylist": [],
  "channel_overrides": {"C0ANNOUNCE": {"human_access": "owner_only", "bot_access": "none"}},

  "trigger": "mention",
  "max_bot_turns": 4,
  "bot_cooldown_seconds": 20,

  "report_channel": "C0OPS",
  "report_thread_ts": "",

  "agent_sessions": true,
  "log_message_text": false
}
```

Every key is described in [configuration.md](configuration.md).
