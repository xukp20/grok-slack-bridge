# Slack "Agents & AI Apps" assistant view

Status: **not enabled yet** (the bot currently runs as a plain bot app, so
Slack labels it *App*; apps with the assistant view enabled are labelled
*Agent*).

## What it gives you

Turning on the assistant view (manifest `features.assistant_view` plus the
`assistant:write` scope) changes how people talk to the bot:

- **A side panel you can open anywhere in Slack.** The bot gets an entry in
  the top bar; clicking it opens a split-view chat next to whatever channel
  you are reading, instead of switching to the bot's DM.
- **One thread per conversation.** Every new chat is its own thread with a
  history list, which keeps unrelated questions apart.
- **Suggested prompts.** A new thread can show a few starter buttons
  (e.g. "Summarise this channel", "Draft a reply").
- **A "thinking…" status** (`assistant.threads.setStatus`) while the agent
  works, instead of only the 👀 reaction, and **thread titles**
  (`assistant.threads.setTitle`).
- **Channel context.** Slack tells the bot which channel you had open when
  you started the thread (`assistant_thread_started` /
  `assistant_thread_context_changed`), so "summarise this" can mean the
  channel you are looking at.

DMs and @mentions keep working exactly as today.

## What enabling it takes

Manifest changes (the owner pastes them into the app config and reinstalls):

```yaml
features:
  assistant_view:
    assistant_description: Chat with your Grok Bot assistant from Slack.
    suggested_prompts: []
oauth_config:
  scopes:
    bot:
      - assistant:write   # added
settings:
  event_subscriptions:
    bot_events:
      - assistant_thread_started          # added
      - assistant_thread_context_changed  # added
```

Bridge changes:

1. Forward `assistant_thread_started` (or answer it locally by setting
   suggested prompts) and remember each thread's context channel.
2. Mark payloads from assistant threads (`conversation: "assistant"`,
   `context_channel`) and always reply in that thread.
3. On receipt call `assistant.threads.setStatus` ("is thinking…") and let
   `reply.sh` clear it, alongside / instead of the 👀 reaction.
4. Optional: set a thread title from the first message.

Notes: the context channel is only read when the bot can see it (it is a
member, or the owner asks via the Grok connector); non-owners still get
general help only. Slack may restrict this feature on some workspace plans.
