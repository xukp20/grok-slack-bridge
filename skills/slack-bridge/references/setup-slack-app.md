# Creating the Slack app

One-time, done by a Slack workspace member who may install apps.

1. Open <https://api.slack.com/apps> → **Create New App** → **From a manifest**
   → pick the workspace → paste `manifest/slack-app-manifest.yaml` (YAML tab)
   or the JSON variant → **Create**.
   - Different name? `scripts/slackctl.sh render-manifest --name "My Bot"`
     (or `python3 scripts/slackctl.py render-manifest …` before installing).
   - The icon is set later under **Basic Information → Display Information**.
2. **Basic Information → App-Level Tokens → Generate Token and Scopes**:
   name it e.g. `socket`, add scope `connections:write`, generate.
   This is `SLACK_APP_TOKEN` (`xapp-…`).
3. **Install App → Install to Workspace** → Allow. Copy the
   **Bot User OAuth Token**: this is `SLACK_BOT_TOKEN` (`xoxb-…`).
4. **App Home → Show Tabs**: confirm *Messages Tab* is on and tick
   *Allow users to send Slash commands and messages from the messages tab*
   (the manifest sets this, but some workspaces show it unticked).
5. Provide both tokens to the agent's environment (secret store / env vars),
   together with `GROK_WEBHOOK_URL` and `GROK_WEBHOOK_AUTH`.
6. `scripts/doctor.sh`, then `scripts/start.sh`.
7. Test: DM the bot → "Working…" (or 👀 without the agent view) → the agent replies in the conversation thread. In a channel, run
   `/invite @Grok Bot`, then `@Grok Bot hello` → reply lands in the thread.

## Scopes and why

| Scope | Used for |
| --- | --- |
| `app_mentions:read` | receive `@bot` mentions in channels |
| `im:history`, `im:read`, `im:write` | receive and answer DMs |
| `chat:write` | post replies |
| `channels:history`, `groups:history`, `mpim:history` | read thread context (`slackctl.sh thread`) in public/private channels and group DMs |
| `reactions:write` | 👀 receipt / ✅ done reactions |
| `users:read` | include the sender's name in payloads, verify the owner ID |
| `assistant:write` | Slack agent features: sessions ("Working…" + Stop), titles, context events ([agent-view](../../../docs/agent-view.md)) |

Events: `app_mention`, `message.im`, plus the agent events
`app_context_changed`, `agent_session_stopped`, `agent_session_title_changed`
(drop them and the scope with `render-manifest --no-agent-view` for a plain
bot). Plain channel messages are not
forwarded unless they mention the bot, so the bot never reads a channel
uninvited.

If you later add scopes or events, Slack asks you to **reinstall** the app;
the bot token usually stays the same, but re-check with `doctor.sh`.
