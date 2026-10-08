# Channel memory file template

A dedicated channel agent has no memory across routine runs, so each channel
keeps a small Markdown file that every run reads first and appends to after
answering. Location: `<INSTALL_DIR>/state/channels/<CHANNEL_ID>.md` (the
reference install is `/workspace/slack-bot`; `state/` is runtime data, not
part of the repository). Whoever routes the channel (normally the owner's main
conversation) writes the Background section once; the dedicated agent then
appends Notes.

Rules: no secrets, tokens, webhook values or private data about the owner;
short dated lines with timezone; move anything still open into "Open
promises" so it is not lost in the notes; trim old notes when the file grows
past a few hundred lines (keep Background and open promises).

Copy from here:

```markdown
# <CHANNEL_NAME> (<CHANNEL_ID>) channel memory

Read this file at the start of every run; append short dated notes after answering.

## Background (written <YYYY-MM-DD> by <WHO>)
- Owner: <OWNER_NAME> (<OWNER_ID>), writes <LANGUAGE>.
- Purpose: <PURPOSE>. <WHAT DOES NOT BELONG HERE AND WHERE IT GOES>.
- Members / other bots: <NAME (bot user U…)>, …
- Access: <human_access / bot allowlist status for this channel, scope, expiry>.
  Only the owner changes access; don't extend or change it yourself.
- Routing: from <YYYY-MM-DD HH:MM TZ> this channel is routed to the dedicated agent
  "<AGENT_LABEL>" (busy_policy <interrupt_merge|queue>).

## Open promises
- <YYYY-MM-DD>: <who was promised what, by whom; remove when done>

## Notes
- <YYYY-MM-DD HH:MM TZ>: <thread ts> <who asked what> → <what was answered / decided>.
```

## Worked example (the #my-bots setup, abridged)

```markdown
# #my-bots (C0C79RD02AK) channel memory

## Background (written 2026-10-08 by the main Grok Bot)
- Owner: <owner name> (<owner ID>), writes Simplified Chinese.
- Purpose: mainly bot-to-bot communication among the owner's bots. Dev progress
  reports go to the owner's DM with the main Grok Bot, not here.
- Other bot here: GPT Dot (bot user U…).
- Bot access: dot is on a pilot allowlist limited to one thread, max 3 bot turns,
  expiring 2026-10-09 09:00 (UTC+8). Don't extend or change it yourself.
- From 2026-10-08 ~22:25 (UTC+8), #my-bots is routed to the dedicated agent
  "Grok Bot #my-bots" instead of the main conversation.

## Notes
- 2026-10-08 22:23: created webhook routine "Grok Bot #my-bots Slack inbox".
- 2026-10-08 22:28: owner asked "which bot are you"; answered: the dedicated agent
  for this channel since ~22:25.
```
