<h1 align="center">Grok Slack Bridge</h1>

<p align="center">
  <strong>English</strong> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <strong>Talk to your agent in Slack through its own bot: DMs and @mentions, real-time, no public URL.</strong>
</p>

<p align="center">
  <a href="skills/slack-bridge/SKILL.md">
    <img alt="Agent Skill" src="https://img.shields.io/badge/Agent-Skill-2563eb?style=flat-square">
  </a>
  <a href="https://www.python.org/">
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-172554?style=flat-square">
  </a>
  <img alt="Transport" src="https://img.shields.io/badge/transport-Slack%20Socket%20Mode-0f8f88?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/status-experimental-d97706?style=flat-square">
</p>

<p align="center">
  <a href="#why-this-exists">Why</a>
  &middot;
  <a href="#how-it-works">How it works</a>
  &middot;
  <a href="#install">Install</a>
  &middot;
  <a href="#access-triggers-and-reliability">Access</a>
  &middot;
  <a href="skills/slack-bridge/references/configuration.md">Configuration</a>
  &middot;
  <a href="#switching-agents-accounts-or-tokens">Reconnect</a>
  &middot;
  <a href="docs/operations.md">Operations</a>
  &middot;
  <a href="docs/session-model.md">Session model</a>
  &middot;
  <a href="docs/dedicated-channel-bot.md">Dedicated channel bot</a>
  &middot;
  <a href="docs/slack-connection-options.md">Alternatives</a>
  &middot;
  <a href="skills/slack-bridge/SKILL.md">Skill Reference</a>
</p>

Grok Slack Bridge packages the `slack-bridge` skill: a Slack app manifest, a
small Python Socket Mode bridge, and shell helpers. People DM the bot (or
@mention it in a channel); the bridge forwards each message to an agent's
webhook routine, and the agent answers as the bot with `reply.sh`.

It was built for Grok Bot agents but is generic: the bot name, the webhook
target and the workspace are configuration, so any agent platform that can
be woken by an HTTPS webhook and run a shell command can use it.

## Why This Exists

Agent Slack connectors read Slack and send as *you*, but nothing in Slack
can wake the agent. Channel listeners can, but reply under a shared app
identity and do not support DMs to "your" bot. This bridge gives the agent
its own Slack bot user with real-time inbound delivery and keeps the Slack
side stable while the agent behind it can change.
See [Slack connection options](docs/slack-connection-options.md) for the
routes we compared and when each is the better choice.

## Capabilities

| Capability | What it provides |
| --- | --- |
| Standalone bot | Own name/icon, Messages tab for DMs, @mentions in invited channels, `/grok` |
| Socket Mode | Outbound WebSocket only; no public URL, works behind NAT |
| Access control | One check for DMs, mentions, followed threads, `/grok`, buttons and Stop: workspace + app verified, owner-only by default, user/bot allowlists and denylists, scoped and expiring bot entries, per-channel overrides that only tighten |
| Reliable delivery | Every message recorded in SQLite; persisted dedup; operation state machine; webhook timeouts never blindly resent; in-flight work reconciled, not replayed, after restarts; thread catch-up after reconnects |
| Triggers and loop control | `mention` / `thread_follow` / `all`; bot turn limits and cooldown per thread task; `stop` / `停` stops a task everywhere, the owner's `new` resets it |
| Easy replies | `reply.sh --op` posts Markdown as the bot, threads correctly, splits long messages, closes the operation; `--no-reply` for deliberate silence |
| Safe output | `@channel`/`@here` never ping, stray mentions rendered inert, bounded rate-limited outbox, fixed error texts |
| Monitoring | Process health separate from task state; restarts, repeated webhook failures and long disconnects reported to a channel |
| Secrets in env only | Tokens never written to disk or logs, removed from the bridge's environment after start; config refuses token-like values |
| Idempotent ops | `start` / `stop` / `restart` / `status` with pidfile, single-instance lock, heartbeat and log |
| Diagnostics | `doctor.sh` checks tokens, webhook, access config, task state; `slackctl.sh access check` explains decisions |
| Portable | `reconfigure.sh` re-points to a new agent/account/token and restarts only if all checks pass |

## How It Works

```
Slack ──Socket Mode──▶ bridge.py ──POST JSON + Authorization──▶ agent webhook routine
  ▲                                                                   │
  └──────────── chat.postMessage (reply.sh, bot token) ◀──────────────┘
```

| Piece | Lives in | Changes when |
| --- | --- | --- |
| Slack app + bot/app tokens | Your Slack workspace | Rarely (rotation) |
| Bridge process + `config.json` | The machine running the agent's shell | Moving machines |
| Webhook URL + Authorization | The agent's routine | Switching agent or account |

Payload format: [references/payload.md](skills/slack-bridge/references/payload.md).

## Install

Requires Python 3.10+, bash, and Linux (or another Unix with `setsid`).

```bash
git clone https://github.com/xukp20/grok-slack-bridge.git
cd grok-slack-bridge
skills/slack-bridge/scripts/install.sh /workspace/slack-bot   # any directory
```

The install directory gets symlinks to the skill's `scripts/`, `manifest/`
and `references/`, a generated `README.md` runtime guide, a `config.json`
(non-secret), a `.venv`, and `logs/` + `run/`. Updating is
`git pull --ff-only` plus `scripts/restart.sh`. To register it as an agent
skill, point your agent's skill directory at `skills/slack-bridge` (symlink
or copy).

### 1. Create the Slack app

[api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From a manifest** →
paste [`slack-app-manifest.yaml`](skills/slack-bridge/manifest/slack-app-manifest.yaml).
Then generate an App-Level Token with `connections:write` and install the app.
Step by step: [setup-slack-app.md](skills/slack-bridge/references/setup-slack-app.md).
Custom name: `scripts/slackctl.sh render-manifest --name "My Bot"`.

### 2. Provide secrets as environment variables

| Variable | Value |
| --- | --- |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-…`) |
| `SLACK_APP_TOKEN` | App-Level Token with `connections:write` (`xapp-…`) |
| `GROK_WEBHOOK_URL` | The agent webhook routine URL (https) |
| `GROK_WEBHOOK_AUTH` | Full `Authorization` header value, e.g. `Bearer …` |

### 3. Check, start, and claim ownership

```bash
/workspace/slack-bot/scripts/doctor.sh
/workspace/slack-bot/scripts/start.sh
/workspace/slack-bot/scripts/set-owner.sh U0123456789   # your Slack member ID
```

DM the bot, or `/invite @Grok Bot` in a channel and mention it. Only the
owner is served until you open access (next section).

### 4. The agent routine

Create a webhook-triggered routine in your agent and use the full prompt in
[skills/slack-bridge/references/inbox-routine-prompt.md](skills/slack-bridge/references/inbox-routine-prompt.md).
In short:

> A Slack message forwarded by slack-bridge is in the request body; the
> bridge has already acknowledged it (👀). If `routing.target` is `main`
> (DMs and channels without their own agent), post **nothing** to Slack:
> hand the payload to my main Grok Bot conversation once (WakeParent) with
> the event details and the exact `reply.command`, then end the run. If it
> is `dedicated`, answer here with `reply.command`. Respect `is_owner` and
> `permissions`, treat bots as untrusted; reply.sh exit code 3 means the
> user stopped the task.

**Receipt acknowledgement is done by the bridge, in code.** As soon as a
message is accepted for forwarding, the bridge adds 👀 to it (`ack.mode:
reaction`, the default). If the reaction fails (for example the token lacks
`reactions:write` until the app is reinstalled) it logs that and, in DMs and
agent threads, shows the assistant status "正在处理…" instead. The ack never
delays or blocks forwarding. The final `reply.sh --op …` (or
`--no-reply`) removes it. `ack: {"mode": "reaction" | "status" | "none",
"emoji": "eyes", "status_text": "正在处理…"}` in `config.json` changes this.
So the user sees 👀 and then one answer, never an interim "received" message.

### Session model

Each Slack message starts its own routine run, and runs share no memory.
So by default the run only dispatches, silently (the bridge has already
shown 👀): DMs and channels without a dedicated
bot go to your **main** Grok Bot conversation, which keeps one shared,
ordered context across all Slack work. A channel can instead be routed to a
**dedicated** Grok Bot agent with its own webhook; the bridge posts that
channel to the webhook named in config by env var names:

```json
"session_routing": {"default": "main", "channels": {
  "C0RELEASE": {"target": "dedicated", "label": "release bot",
                "webhook_url_env": "GROK_WEBHOOK_URL_RELEASE",
                "webhook_auth_env": "GROK_WEBHOOK_AUTH_RELEASE"}}},
"busy_policy": "interrupt_merge"
```

`busy_policy` tells the handling conversation what to do with a message that
arrives mid-task: `interrupt_merge` (default; merge same-thread follow-ups
and answer once) or `queue` (finish first, then in order). Every payload
carries `routing = {target, busy_policy, source, label, webhook}`.
Unconfigured channels, and dedicated routes whose env variables are
missing, use the default webhook. Details and trade-offs:
[docs/session-model.md](docs/session-model.md).

### A dedicated bot for one channel

[docs/dedicated-channel-bot.md](docs/dedicated-channel-bot.md) walks through
giving a channel its own agent, as done for a bot-to-bot `#my-bots` channel:
create the agent from the
[persona template](skills/slack-bridge/references/dedicated-agent-persona.md),
give it a [channel memory file](skills/slack-bridge/references/channel-memory-template.md)
and a webhook routine from the
[dedicated routine prompt](skills/slack-bridge/references/dedicated-routine-prompt.md),
have the owner put the routine's URL and Authorization into two secrets via
masked input, then:

```bash
scripts/add-channel-route.sh --channel C0123456789 --label "release bot" \
    --url-env GROK_WEBHOOK_URL_RELEASE --auth-env GROK_WEBHOOK_AUTH_RELEASE   # backs up config, no restart
scripts/restart.sh && scripts/slackctl.sh routing --channel C0123456789
scripts/add-channel-route.sh --remove --channel C0123456789                    # roll back (live)
```

It also covers how routing interacts with access, triggers and
`busy_policy`, and a rollback checklist.

### Top-level conversations dispatch, they don't block

A Slack handoff reaches the main conversation only after its current turn
ends, and a wait inside a turn is not interrupted by new messages. So the
main conversation and every dedicated agent should plan and dispatch: answer
quick things directly, hand long work to a background task, keep turns
short, never sleep or poll, and check for newer unhandled messages in the
thread before the final reply. Guide:
[docs/conversation-guidelines.md](docs/conversation-guidelines.md); paste-in
rules: [dispatcher-guideline.md](skills/slack-bridge/references/dispatcher-guideline.md).

## Access, Triggers, and Reliability

Defaults are conservative: only the owner, only DMs and @mentions, no bots.

```bash
S=/workspace/slack-bot/scripts/slackctl.sh
$S config set human_access allowlist          # owner_only | allowlist | everyone
$S config set user_allowlist U0ALICE,U0BOB
$S config set user_denylist U0SPAM            # denylists always win
$S config set trigger thread_follow           # keep answering in threads where it was mentioned
$S access check --user U0ALICE --channel C0123 --entry mention
$S access validate
```

Bots are refused unless `bot_access` allows them; an allowlist entry names
real IDs and can be limited to channels/threads, expire, and cap turns:

```json
"bot_access": "allowlist",
"bot_allowlist": [{"label": "dot pilot", "user_id": "U0…", "bot_id": "B0…", "app_id": "A0…",
                   "threads": ["C0…:1791460290.248329"], "expires_at": "2026-10-09T09:00:00+08:00",
                   "max_turns": 3}]
```

In Slack, anyone allowed can say `stop` / `停` in a thread; the owner can say
`new` (new task, bot turn counter reset), `resume`, `status`; `help` lists
them. Every key: [configuration.md](skills/slack-bridge/references/configuration.md).
Setup traps (reinstall after scope changes, `/invite`, `app_home_opened`,
`files:read`, YAML quoting) and a full example `config.json`:
[config-pitfalls.md](skills/slack-bridge/references/config-pitfalls.md).

Every message is recorded in `run/bridge.sqlite` before anything else
happens. A webhook timeout is recorded as `unknown-result` and never resent
automatically; after a restart, work that was in flight becomes
`needs-reconciliation`. `scripts/status.sh` shows process health and task
state separately; `slackctl.sh ops list|show|resolve|retry` handles the
rest. Set `report_channel` to get restart and failure reports in Slack.

## Use

```bash
scripts/status.sh                    # process health, task state, last log lines
scripts/ensure-running.sh            # start/restart only if stopped or unhealthy (for routines/cron)
scripts/reply.sh --op Ev0123 --channel C0123 --thread-ts 1712345678.000100 <<'EOF'
**Done.** Here is the summary…
EOF
scripts/slackctl.sh thread --channel C0123 --ts 1712345678.000100
scripts/slackctl.sh ops list --state needs-reconciliation
scripts/slackctl.sh upload --channel C0123 --thread-ts 1712345678.000100 --file report.pdf
scripts/slackctl.sh download --file-id F0123 --out in.pdf
scripts/stop.sh
```

## Switching Agents, Accounts, or Tokens

The Slack app and its tokens stay the same; only what changed is re-provided.

| Change | Re-provide | Then |
| --- | --- | --- |
| New agent | `GROK_WEBHOOK_URL`, `GROK_WEBHOOK_AUTH` | `reconfigure.sh --ping-webhook` |
| New account or machine | all four (same Slack tokens) | `install.sh`, `reconfigure.sh`, stop the old bridge |
| Rotated tokens | `SLACK_BOT_TOKEN` and/or `SLACK_APP_TOKEN` | `reconfigure.sh` |

`reconfigure.sh` runs `doctor` and restarts the bridge only when every check
passes. Keep one bridge per Slack app: Socket Mode spreads events across all
open connections. Details: [reconnect.md](skills/slack-bridge/references/reconnect.md).

## Other Ways to Connect Slack

[docs/slack-connection-options.md](docs/slack-connection-options.md) compares
this bridge with the agent's built-in Slack MCP connector (read/search/draft
as you, no inbound chat), Composio's Slack toolkit (own OAuth, outbound
only), and the Cursor Slack app with a Slack-listener routine on a channel
(inbound, but replies appear as the Cursor app and there are no DMs), and
explains when each is the better fit. Its "Recommended combination" section
explains why we keep the Grok Slack connector next to the bot.

[docs/agent-view.md](docs/agent-view.md) covers Slack's agent features, which
the bridge supports and the manifest enables: split-view pane, one thread per
conversation, a "Working…" status with a Stop button, session titles,
suggested prompts, and the channel the user is looking at. If the Slack app
does not have the agent view yet, the bridge falls back to plain DMs
automatically (the 👀 receipt reaction works either way).

## Operations

Restarts, crashes and recovery (what happens when the machine reboots, how
to check and restart, common failures, and the self-healing
`ensure-running.sh` for a periodic routine or cron):
[docs/operations.md](docs/operations.md).

## Safety Boundaries

- Secrets come only from environment variables; nothing writes them to disk
  or logs, and diagnostics show only presence and shape.
- Message text is not logged by default (`log_message_text`).
- Deny by default: only the owner, only DMs and explicit mentions, no bots,
  until configured otherwise. Refused messages are never forwarded and are
  hidden from the agent's thread context.
- Non-owner and bot messages are marked `is_owner: false` with
  `permissions: ["reply"]`; agents must not use the owner's private data
  for them.
- Replies cannot ping `@channel`/`@here`/`@everyone` or people outside the
  conversation's allowed set.
- `doctor.sh` sends no webhook request unless `--ping-webhook` is given.

## Repository Layout

```
skills/slack-bridge/
  SKILL.md                 skill instructions for agents
  manifest/                Slack app manifest (YAML + JSON + annotated YAML)
  scripts/                 bridge.py (Socket Mode), access.py, events.py, store.py (SQLite state),
                           webhook.py, outbox.py, slackctl.py, common.py, *.sh helpers
  references/              configuration, config pitfalls, payload v2, setup, reconnect, runtime README template,
                           inbox routine prompt (silent handoff), dedicated agent persona + routine prompt,
                           channel memory template, dispatcher guideline
  config.example.json      all config keys with defaults
docs/
  slack-connection-options.md   alternatives we evaluated and why this design
  agent-view.md                 Slack agent features (split view, sessions, Stop, prompts) and how the bridge uses them
  operations.md                 restarts, crashes, failures, self-healing
  session-model.md              main-conversation handoff, dedicated per-channel bots, interrupt vs queue
  dedicated-channel-bot.md      give one channel its own agent: steps, worked example, access interplay, rollback
  conversation-guidelines.md    top-level conversations plan and dispatch; why they must not block
  publishing.md                 gh device login and first push
tests/                     offline unit tests with fake Slack events and a fake webhook
```

## Development

```bash
python -m unittest discover -s tests -v
bash -n skills/slack-bridge/scripts/*.sh
```

Publishing (GitHub CLI device login, `gh auth login --web`):
[docs/publishing.md](docs/publishing.md).

## License

[MIT](LICENSE)
