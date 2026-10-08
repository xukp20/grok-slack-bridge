# Slack agent view ("Agents" feature)

Status: **supported by the bridge** and enabled in the checked-in manifest
(`features.agent_view`). Apps with this feature are labelled *Agent* in
Slack; plain bot apps are labelled *App*.

Slack has two generations of this feature. The older *Assistant* experience
(`features.assistant_view`, separate Chat/History tabs,
`assistant_thread_started`, `assistant.threads.*`) is being deprecated and
new apps can only use the *Agent* experience (`features.agent_view`). This
bridge implements the Agent experience. Source docs:
[Developing an agent](https://docs.slack.dev/ai/developing-agents),
[Agent sessions](https://docs.slack.dev/ai/agent-sessions),
[Migrating to the Agent messaging experience](https://docs.slack.dev/ai/migrating-to-agent-messaging),
[Split view](https://docs.slack.dev/surfaces/split-view).

## What users get

- **Split view / top-bar entry.** People can open the bot in a pane next to
  whatever channel they are reading instead of switching to its DM.
- **One thread per conversation.** In the Messages tab each new message
  starts its own session thread, listed in a timeline above the composer;
  users can pin, rename, or archive sessions. The bot replies in that thread.
- **"Working…" status with a Stop button** while the agent handles a message
  (the bridge's 👀 receipt reaction is added as well; both end with the reply).
- **Session titles** taken from the first message of the conversation.
- **Suggested prompts** at the top of the Messages tab (up to four, from the
  manifest).
- **What the user is looking at.** `app_context_changed` tells the bridge
  which channel the user has open next to the pane; it is passed to the
  agent as `viewing_context`, so "summarise this channel" has a target.

DMs and @mentions keep working. @mentions in channels also get a session
(status + title) on the mention's thread.

## Manifest

```yaml
features:
  agent_view:
    agent_description: Chat with your Grok Bot assistant from Slack.
    suggested_prompts:            # optional, max 4
      - title: Summarize this channel
        message: Summarize the recent discussion in the channel I'm looking at.
oauth_config:
  scopes:
    bot:
      - assistant:write           # added
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
      - app_home_opened               # added (Slack requires it for agent_view)
      - app_context_changed           # added
      - agent_session_stopped         # added (enables the Stop button)
      - agent_session_title_changed   # added
```

Generate it with your own name and prompts:

```bash
scripts/slackctl.sh render-manifest --name "My Bot" \
  --prompt "Summarize this channel|Summarize the recent discussion in the channel I'm looking at." \
  --prompt "Draft a reply|Help me draft a reply to the latest message."
# --no-agent-view renders the plain bot manifest
```

To enable it on an existing app: **App settings → App Manifest**, paste the
new manifest, save, then **Install App → Reinstall** (new scope + events).
Users may need to reload Slack to see the agent UI. The bot token normally
stays the same; check with `doctor.sh`. Some AI features need a paid Slack
plan (or a Developer Program sandbox).

## How the bridge handles it

| Slack event | Bridge behaviour |
| --- | --- |
| `message.im` / `app_mention` | calls `agents.sessions.setStatus` with `status: processing` (title from the first message, `initiator_user_id` = sender) on the message's thread (`thread_ts`, or the message itself for a new conversation), then forwards the payload with `agent_session` set and `reply.thread_ts` = that thread |
| `app_context_changed` | remembers the channel(s) the user is viewing; added to that user's next payloads as `viewing_context` |
| `agent_session_stopped` | after the same access check as any other entry point: marks the thread `stopped` (queued and accepted operations become `stopped`, so `reply.sh` refuses to post there with exit code 3), sets the session back to `active`, posts `stop_message`, and records the stop so a later `--session-status processing` does not re-open it |
| `app_home_opened` | logged only (Slack requires the subscription for agent view) |
| `agent_session_title_changed` | logged only |

Agent-view events are never forwarded to the webhook, so they do not wake
the agent.

**Automatic fallback.** If Slack refuses the session call (the app has no
agent view yet, e.g. `not_authorized`, or a missing scope), the bridge logs
it once and behaves exactly as before: top-level DM replies, no
"Working…" (the 👀 receipt ack from `ack` works either way). Set `agent_sessions` to `false` in
`config.json` to force the plain behaviour.

**Ending "Working…".** With `agents.sessions.*`, posting a message does not
clear the status by itself. The payload's `reply.command` therefore includes
`--session-status active`. For an interim "on it" message during long work,
reply with `--session-status processing` instead, then send the final answer
with `active`. Use `suspended` when you asked the user a question and are
waiting. A session left in `processing` times out to `active` after an hour.

```bash
scripts/reply.sh --channel D0… --thread-ts 1712… --session-status processing <<'EOF'
On it, give me a minute.
EOF
scripts/slackctl.sh session --channel D0… --thread-ts 1712… --status active [--title "New title"]
```

**Stop cannot interrupt a run that is already working**, but it stops its
output: the operation becomes `stopped`, `reply.sh --op` refuses to post
into the thread (exit code 3), nothing queued is submitted, and later bot
messages in the thread are ignored. Typing `stop` / `停` in the thread does
the same. A new @mention or DM from an allowed person continues the thread.

## Config keys

| Key | Default | Meaning |
| --- | --- | --- |
| `agent_sessions` | `true` | use agent sessions when Slack allows it |
| `session_title_chars` | `60` | max length of auto titles (10–200) |
| `stop_message` | `Stopped.` | posted after Stop (empty = silent) |

## Notes

- The viewing context is only a channel ID. The bot can read that channel
  only if it is a member; for other channels the owner's own Slack
  connector (if the agent has one) is the way to read it. Non-owners still
  get general help only.
- `assistant:write` also allows `assistant.threads.setSuggestedPrompts` for
  dynamic prompts; the bridge uses the static manifest prompts.
