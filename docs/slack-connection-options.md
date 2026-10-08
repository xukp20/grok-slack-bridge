# Slack connection options (and why this repo uses a Socket Mode app)

Before building this bridge we explored every way a Grok Bot agent could be
reached from Slack. They are kept here as alternatives; each is a valid
choice for a different need.

## Summary

| Route | Inbound chat (Slack → agent) | Replies appear as | Setup | Best for |
| --- | --- | --- | --- | --- |
| **A. Standalone Socket Mode app (this repo)** | Yes — DMs and @mentions wake the agent via webhook, near real time | Its own bot user (e.g. *Grok Bot*), own name/icon | Create app from manifest, 2 tokens, 1 webhook, run the bridge | A ChatGPT-style bot people can DM or @mention; shareable with teammates |
| B. Grok Bot Slack MCP connector | No | The user (or a draft for the user to send) | Connect Slack in the app's connectors | Reading/searching Slack, summarising channels, drafting messages as yourself |
| C. Composio Slack / Slackbot toolkit | No (outbound tools only) | The user or a Composio bot identity | Separate Composio OAuth connection per account | Extra Slack API actions through Composio when the native connector lacks them |
| D. Cursor Slack app + Slack listener routine | Yes — messages in the watched channel wake the agent | The **Cursor** app, not a standalone bot | Connect Slack to the Cursor account, `/invite @Cursor`, create a routine with a Slack listener trigger | Quick setup with no tokens or long-running process, a single team channel is enough |

## B. Grok Bot Slack MCP connector

The built-in Slack connector lets the agent read channels, threads, canvases
and profiles, search messages, and create drafts or send messages **as the
connected user**. It is request/response only: nothing in Slack can wake
the agent, so you cannot "chat with" the agent inside Slack through it.

Use it for: catching up on Slack, digests, finding threads, drafting replies
in your own name. It works alongside this bridge (the bridge handles the
inbound chat; the connector is still the best way to read your broader
Slack).

## C. Composio Slack / Slackbot toolkit

Composio exposes many Slack Web API actions (post, read, react, upload…),
optionally under a bot identity. It requires its **own** OAuth connection
(`COMPOSIO_MANAGE_CONNECTIONS`) separate from the agent's Slack connector,
and it provides **outbound tools only** — no event delivery that wakes the
agent. A polling routine (e.g. every 5 minutes) could emulate a chat, at the
cost of latency and wasted runs.

Use it for: Slack actions the native connector does not offer, or when you
already standardise on Composio.

## D. Cursor Slack app with a Slack listener trigger

A Grok Bot routine can use a Slack listener trigger on a channel (we tried
`#ask-bot`): connect Slack to the Cursor account (a connect card appears),
run `/invite @Cursor` in the channel, and every message (or only
@mentions, depending on the trigger) wakes the agent, which replies in the
message's thread. No tokens to manage and no process to keep alive.

Limitations: replies come from the **Cursor** app rather than a bot with its
own name and avatar; people cannot DM "your bot" directly; and anyone in the
channel effectively borrows the owner's agent and permissions unless the
routine prompt restricts that.

Use it for: a quick personal or small-team channel bot when branding and
DMs do not matter.

## Why the standalone Socket Mode app

- **Its own identity**: a real bot user (*Grok Bot*) that people can DM from
  the sidebar or @mention in any channel it is invited to.
- **Real-time inbound**: Socket Mode pushes events instantly; the bridge
  forwards them to a webhook routine — no polling.
- **No public URL**: Socket Mode uses an outbound WebSocket, so it runs on
  any box behind NAT.
- **Portable**: the Slack app and tokens are stable; switching agents or
  accounts only swaps the webhook (`reconfigure.sh`). See
  [skills/slack-bridge/references/reconnect.md](../skills/slack-bridge/references/reconnect.md).
- **Owner-aware**: payloads carry `is_owner`, and `access: owner_only` is one
  config switch, so sharing the bot does not silently share the owner's data.

Trade-offs: you manage two tokens and a long-running process (restart it
after the machine restarts), and the Slack app must be installed by someone
allowed to install apps in the workspace.
