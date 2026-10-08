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
  <a href="#switching-agents-accounts-or-tokens">Reconnect</a>
  &middot;
  <a href="docs/operations.md">Operations</a>
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
| Standalone bot | Own name/icon, Messages tab for DMs, @mentions in invited channels |
| Socket Mode | Outbound WebSocket only; no public URL, works behind NAT |
| Clean forwarding | Immediate ack, bot/self/edit/join filtering, retry dedupe, 👀 receipt, ⚠️ on delivery failure |
| Easy replies | `reply.sh` posts Markdown as the bot, threads correctly, splits long messages, clears 👀 |
| Owner awareness | `is_owner` in every payload; optional `access: owner_only` |
| Secrets in env only | Tokens never written to disk or logs; config refuses token-like values |
| Idempotent ops | `start` / `stop` / `restart` / `status` with pidfile, heartbeat and log |
| Diagnostics | `doctor.sh` checks tokens (`auth.test`, `apps.connections.open`), webhook TLS, optional ping |
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

DM the bot, or `/invite @Grok Bot` in a channel and mention it.

### 4. The agent routine

Create a webhook-triggered routine in your agent whose prompt says roughly:

> A Slack message forwarded by slack-bridge is in the request body. Follow
> `/workspace/slack-bot/README.md`: respect `is_owner`, then answer by running
> the payload's `reply.command`.

## Use

```bash
scripts/status.sh                    # running? heartbeat, counters, last log lines
scripts/ensure-running.sh            # start/restart only if stopped or unhealthy (for routines/cron)
scripts/reply.sh --channel C0123 --thread-ts 1712345678.000100 <<'EOF'
**Done.** Here is the summary…
EOF
scripts/slackctl.sh thread --channel C0123 --ts 1712345678.000100
scripts/slackctl.sh config set access owner_only
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
does not have the agent view yet, the bridge falls back to plain DMs and the
👀 reaction automatically.

## Operations

Restarts, crashes and recovery (what happens when the machine reboots, how
to check and restart, common failures, and the self-healing
`ensure-running.sh` for a periodic routine or cron):
[docs/operations.md](docs/operations.md).

## Safety Boundaries

- Secrets come only from environment variables; nothing writes them to disk
  or logs, and diagnostics show only presence and shape.
- Message text is not logged by default (`log_message_text`).
- Plain channel messages are never forwarded; only DMs and explicit mentions.
- Non-owner messages are marked `is_owner: false`; agents must not use the
  owner's private data for them. `access: owner_only` blocks them entirely.
- `doctor.sh` sends no webhook request unless `--ping-webhook` is given.

## Repository Layout

```
skills/slack-bridge/
  SKILL.md                 skill instructions for agents
  manifest/                Slack app manifest (YAML + JSON)
  scripts/                 bridge.py, slackctl.py, common.py, *.sh helpers
  references/              setup, reconnect, payload, runtime README template
  config.example.json      all config keys with defaults
docs/
  slack-connection-options.md   alternatives we evaluated and why this design
  agent-view.md                 Slack agent features (split view, sessions, Stop, prompts) and how the bridge uses them
  operations.md                 restarts, crashes, failures, self-healing
  publishing.md                 gh device login and first push
tests/                     unit tests (standard library only)
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
