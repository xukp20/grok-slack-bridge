# Dedicated channel bot: give one Slack channel its own agent

By default every DM and channel goes to the owner's **main conversation**
([session-model.md](session-model.md)). A channel with its own audience or
rhythm, such as a bot-to-bot channel, a release channel or a team support
channel, can instead be routed to a **separate agent** with its own
instructions, memory file and webhook routine. The Slack app, bot user and
tokens stay the same. Only that channel's messages go to a different webhook.

```
                         ┌─▶ default webhook ──▶ main conversation   (DMs, other channels)
Slack ─▶ bridge (access, │
         👀, routing) ───┤
                         └─▶ GROK_WEBHOOK_URL_<X> ──▶ dedicated agent's routine (<CHANNEL_ID> only)
```

Reusable pieces (all placeholders, no secrets):

| File | What it is |
| --- | --- |
| [references/dedicated-agent-persona.md](../skills/slack-bridge/references/dedicated-agent-persona.md) | Instructions/persona for the dedicated agent |
| [references/dedicated-routine-prompt.md](../skills/slack-bridge/references/dedicated-routine-prompt.md) | Its webhook routine prompt (answer dedicated payloads in the run, end silently otherwise) |
| [references/channel-memory-template.md](../skills/slack-bridge/references/channel-memory-template.md) | The channel memory file it reads and appends every run |
| [references/dispatcher-guideline.md](../skills/slack-bridge/references/dispatcher-guideline.md) | Plan-and-dispatch rules for every top-level conversation ([why](conversation-guidelines.md)) |
| `scripts/add-channel-route.sh` / `slackctl.sh route add\|remove` | Backs up `config.json`, writes or removes the route, checks the env vars without printing them, prints next steps; restarts only with `--restart` |

## When to use one

| Use a dedicated agent when | Stay on the main conversation when |
| --- | --- |
| The channel has its own purpose, members or bots, and its context would clutter the main conversation | The owner wants one shared context ("what did I ask in the DM earlier?") |
| Messages there should not wait behind the owner's long tasks | Messages are rare or mostly from the owner |
| Different instructions apply (tone, language, what may be shared) | You only want a different `busy_policy`: use a `target: "main"` entry instead |

The dedicated agent shares the machine (files, installed tools, the bridge
scripts) and the owner's shared memory with the main conversation, but not
its chat history or private memory. Write what it needs into its
instructions and the channel memory file.

## Step by step

Roles: **owner** (the human who controls the agents and Slack), **main**
(the owner's main conversation, which sets it up), **dedicated** (the new
agent).

1. **Pick the channel and names.** Channel ID (`C…`/`G…`; in Slack: channel
   name → About → Channel ID), a label (`<AGENT_LABEL>`), and two env var
   names that are not the default ones, `GROK_WEBHOOK_URL_<X>` /
   `GROK_WEBHOOK_AUTH_<X>`. Make sure the bot is in the channel
   (`/invite @Grok Bot`).
2. **Create the dedicated agent** (main). Give it the instructions from
   [dedicated-agent-persona.md](../skills/slack-bridge/references/dedicated-agent-persona.md)
   with the placeholders filled in: channel purpose and owner, replies only
   through `reply.command`, read and append the channel memory file every
   run, the safety rules (owner-only private data; others and bots get
   general help; never print secrets), short bot-to-bot exchanges, and the
   dispatcher rules.
3. **Write the channel memory file** (main):
   `<INSTALL_DIR>/state/channels/<CHANNEL_ID>.md` from
   [channel-memory-template.md](../skills/slack-bridge/references/channel-memory-template.md).
   Include the background the dedicated agent cannot know: purpose, owner,
   other bots, the current bot allowlist status, open promises made in the
   channel, and the routing date.
4. **Create its webhook routine** (dedicated; main asks it to). Use
   [dedicated-routine-prompt.md](../skills/slack-bridge/references/dedicated-routine-prompt.md).
   Name it clearly, e.g. "<AGENT_LABEL> Slack inbox". It should report its
   exact name back and post nothing to Slack while being set up.
5. **Hand over the secrets** (owner). The owner opens that routine's panel,
   copies the webhook URL and the Authorization header value, and enters
   them through **masked secret inputs** as box secrets named
   `GROK_WEBHOOK_URL_<X>` and `GROK_WEBHOOK_AUTH_<X>`. Never paste them into
   chat. Saved secrets reach new processes automatically on a Grok Bot box.
   Elsewhere, export them in the shell that starts the bridge.
6. **Route the channel** (main). Back up and write the route:

   ```bash
   /workspace/slack-bot/scripts/add-channel-route.sh --channel <CHANNEL_ID> \
       --label "<AGENT_LABEL>" --url-env GROK_WEBHOOK_URL_<X> --auth-env GROK_WEBHOOK_AUTH_<X> \
       [--busy-policy queue]            # optional per-channel policy
   ```

   It refuses values that look like URLs, tokens or `Bearer …` headers
   (without echoing them), refuses the default variable names, checks that
   both variables are set (prints only `set`/`missing`), refuses to
   overwrite a different existing entry without `--replace`, keeps every
   other channel entry, backs up `config.json` to `config.json.bak-<epoch>`
   and prints the next steps. `--dry-run` shows the entry without writing.
   Equivalent by hand: copy `config.json`, then
   `slackctl.sh config set session_routing '<whole object>'`. Note that
   `config set` **replaces the whole `session_routing` object**, so include
   the existing channel entries.
7. **Restart** (main), only when `slackctl.sh ops list` shows nothing in
   flight: `scripts/restart.sh` (or re-run step 6 with `--restart`). The
   bridge reads secrets only at start, so a new variable needs a restart.
   Later config-only edits apply live.
8. **Verify.**
   - `slackctl.sh routing --channel <CHANNEL_ID>`: `target: "dedicated"`,
     `webhook: "dedicated"`, no `fallback`.
   - `doctor.sh`: a `route <CHANNEL_ID>` line with TLS ok (no request is
     sent).
   - `status.sh` / `run/heartbeat.json`: `dedicated_routes` lists the
     channel.
   - Real test: the owner @mentions the bot in the channel. The dedicated
     agent answers once, 👀 disappears, and `slackctl.sh ops list` shows the
     operation `completed`. A DM still goes to the main conversation.
9. **Tell the main conversation's memory** that this channel is no longer
   its own, so it doesn't answer there out of habit.

## Worked example: #my-bots (2026-10-08, UTC+8)

| Item | Value |
| --- | --- |
| Channel | `#my-bots`, `C0C79RD02AK`, mostly bot-to-bot (the owner's bots, e.g. a GPT Dot agent) |
| Agent | "Grok Bot #my-bots": instructions as in the persona template; dev progress goes to the owner's DM, not here |
| Routine | "Grok Bot #my-bots Slack inbox", webhook-triggered, prompt adapted from the inbox prompt as in the dedicated template |
| Secrets | `GROK_WEBHOOK_URL_MYBOTS`, `GROK_WEBHOOK_AUTH_MYBOTS`, entered by the owner through masked inputs |
| Memory | `/workspace/slack-bot/state/channels/C0C79RD02AK.md`: purpose, owner, other bots, bot allowlist status (dot pilot limited to one thread), an open promise, routing date, then Notes |

What was run (by the main conversation, after backing up `config.json`):

```bash
S=/workspace/slack-bot/scripts
$S/slackctl.sh config set session_routing '{"default":"main","channels":{"C0C79RD02AK":{"target":"dedicated","label":"Grok Bot #my-bots","webhook_url_env":"GROK_WEBHOOK_URL_MYBOTS","webhook_auth_env":"GROK_WEBHOOK_AUTH_MYBOTS"}}}'
$S/restart.sh
$S/slackctl.sh routing --channel C0C79RD02AK   # dedicated, webhook dedicated
$S/doctor.sh                                   # route C0C79RD02AK: TLS ok
```

With the helper it's one command:

```bash
$S/add-channel-route.sh --channel C0C79RD02AK --label "Grok Bot #my-bots" \
    --url-env GROK_WEBHOOK_URL_MYBOTS --auth-env GROK_WEBHOOK_AUTH_MYBOTS --restart
```

Then the owner @mentioned the bot in #my-bots ("你是哪个Bot"). The dedicated
agent answered that it is the channel's dedicated agent and appended a note
to the memory file. DMs kept going to the main conversation.

Lesson from the same round: a Slack handoff reaches the main conversation
only **after its current turn ends**, and a blocking wait inside a turn is
not interrupted by new messages. That is why every top-level conversation,
main or dedicated, should plan and dispatch rather than block. See
[conversation-guidelines.md](conversation-guidelines.md).

## How routing interacts with access, triggers and busy_policy

Order inside the bridge: **access → trigger/loop limits → ack (👀) →
routing → webhook**. Routing only chooses *which* agent gets a message that
was already accepted. It never grants access, and the dedicated agent cannot
widen it.

- **`human_access` / `user_allowlist` / denylists** apply to the channel as
  everywhere. To make a channel stricter, use `channel_overrides["C…"]`
  (`human_access`, `bot_access`, extra denylists, allowlists that must
  *also* match, lower `max_bot_turns`, higher `bot_cooldown_seconds`).
  Overrides only tighten. A looser value is ignored and `doctor` warns.
- **Bots** (`bot_access`, `bot_allowlist`): a bot-to-bot channel still
  needs each bot allow-listed by real IDs. Scope the entry to the channel
  (`"channels": ["C…"]`) or to threads, and give it `expires_at` and
  `max_turns`. Only the owner widens this. The dedicated agent's
  instructions say so.
- **Triggers**: `trigger` can be set per channel in `channel_overrides`
  (it is behaviour, not access). `thread_follow` suits a conversational
  channel. `all` makes the agent see every message, so use it only in a
  channel meant for that.
- **`busy_policy`** per channel lives in the route entry
  (`"busy_policy": "queue"`), or in a `{"target": "main", "busy_policy": …}`
  entry for a channel that stays on main. `interrupt_merge` (default) suits
  conversational channels where follow-ups correct the request. `queue`
  suits channels of independent requests and bot-to-bot channels, where
  merging two bots' turns would be wrong.
- **Health check**: none needed for the dedicated agent. The bridge is
  shared by every route, so the owner's single
  [health-check routine](../skills/slack-bridge/references/health-check-routine-prompt.md)
  covers this channel too. Do not create a second one; two checks only
  double the cost and can restart the bridge twice.
- **Stop / new / resume**, receipts, dedup, busy-retry and catch-up work
  the same for every route. `stop` in the channel marks the thread stopped,
  and the dedicated agent's `reply.sh` then exits 3.
- **Payload**: `is_owner`, `permissions` and `actor_type` are the same as on
  main, and `routing` says `{"target": "dedicated", "source": "channel",
  "label": …, "webhook": "dedicated"}`.

Check one decision without Slack:
`slackctl.sh access check --user U… [--bot-id B… --app-id A…] --channel C… --entry mention`,
then `slackctl.sh routing --channel C…`.

## Re-pointing (same channel, new agent or rotated routine key)

Keep the env var names. The owner enters the new URL/Authorization into the
same two secrets. Then `restart.sh` and `routing`/`doctor` as in step 8. No
config change is needed.

## Removing or rolling back the route: checklist

1. Remove the entry (live, no restart):
   `scripts/add-channel-route.sh --remove --channel <CHANNEL_ID>`
   (= `slackctl.sh route remove --channel <CHANNEL_ID>`, which backs up
   first). Or restore a backup: `cp config.json.bak-<epoch> config.json`.
   That is also live unless it adds a route with new variables.
2. `slackctl.sh routing --channel <CHANNEL_ID>`: `target: "main"`,
   `source: "default"`.
3. `slackctl.sh ops list --state accepted --state unknown-result
   --state needs-reconciliation`: resolve anything the dedicated agent left
   open for that channel (`ops resolve <op> --to completed|no_reply|ignored`).
4. Test: @mention the bot in the channel. The main conversation answers.
5. Tell the main conversation the channel is back, and point it at the
   channel memory file if it should keep that context. Add a dated note to
   the file ("routed back to main").
6. Owner: pause or delete the dedicated agent's webhook routine, and delete
   the two secrets if they won't be reused. Until the next bridge restart
   the bridge still holds the old values in memory but no longer uses them.
7. Optional: revert the channel's `channel_overrides` / bot allowlist
   scope if they were only for the dedicated agent.

If the env variables go missing (deleted secrets, a restart from a shell
without them), the bridge doesn't drop messages. It logs a warning and
delivers the channel to the default webhook with `routing.fallback` set, so
the main conversation handles it. Check `doctor.sh` if a dedicated channel
is suddenly answered by main.
