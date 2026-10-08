# Reconnecting, switching agents/accounts, rotating tokens

The design keeps the **Slack app** (and therefore its name, icon, DM
history, channel memberships and tokens) as the stable part. The only thing
tied to a particular agent is the webhook it forwards to.

| What changed | Slack app | `SLACK_*` tokens | `GROK_WEBHOOK_*` | Action |
| --- | --- | --- | --- | --- |
| Different agent, same account and machine | keep | keep | **new** | export new webhook values → `reconfigure.sh` |
| Different account / new machine | keep | **re-provide the same values** to the new environment | **new** | install there → export all four → `reconfigure.sh`; stop the old bridge |
| Token rotated or leaked | keep | **new** | keep | regenerate in Slack → export → `reconfigure.sh` |
| Webhook URL/secret regenerated | keep | keep | **new** | export → `reconfigure.sh` |

`reconfigure.sh` runs `doctor` (bot token via `auth.test`, app token via
`apps.connections.open`, webhook TLS reachability, optional
`--ping-webhook`) and restarts the bridge **only if every check passes**, so
a half-configured switch never takes down a working bridge.

## Switch to another agent (same machine)

1. In the new agent, create a webhook-triggered routine with the prompt in
   [inbox-routine-prompt.md](inbox-routine-prompt.md) (silent handoff to the
   main conversation; the bridge itself shows 👀). Copy its URL and
   Authorization header.
2. Export `GROK_WEBHOOK_URL` and `GROK_WEBHOOK_AUTH` with the new values
   (secret store / env, never a file).
3. `scripts/reconfigure.sh --agent-label "new agent name" --ping-webhook`.
4. Disable or delete the old agent's routine.

## Move to another account or machine

1. Clone the repo there and run `skills/slack-bridge/scripts/install.sh <dir>`.
2. Provide the **same** `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` (copy them
   again from the Slack app pages; nothing needs to change in Slack) plus the
   new webhook values.
3. `scripts/set-owner.sh U…` (or copy the old `config.json`, which holds no secrets).
4. `scripts/reconfigure.sh --ping-webhook`.
5. **Stop the old bridge** (`scripts/stop.sh` on the old machine, or let it die).
   Slack Socket Mode spreads events across *all* open connections of an app,
   so two live bridges would each receive roughly half of the messages.

## Rotate tokens

- Bot token: **OAuth & Permissions → Revoke tokens** (or reinstall) and copy
  the new `xoxb-` token.
- App token: **Basic Information → App-Level Tokens** → generate a new one
  with `connections:write`, then revoke the old one.
- Export the new value(s) and run `scripts/reconfigure.sh`.

## Troubleshooting

Full runbook (restarts, crashes, self-healing): `docs/operations.md` at the
repository root.

| Symptom | Check |
| --- | --- |
| No 👀 on new messages | `status.sh`; bridge not running or app not invited to the channel (`/invite @bot`) |
| 👀 then ⚠️ | webhook rejected or unreachable → `doctor.sh --ping-webhook`, update `GROK_WEBHOOK_*` |
| `invalid_auth` | token revoked/rotated → re-export and `reconfigure.sh` |
| `not_allowed_token_type` | `SLACK_APP_TOKEN` must be the `xapp-` app-level token |
| DMs say "Sending messages to this app has been turned off" | App Home → enable the Messages tab checkbox |
| Only some messages arrive | another bridge is connected with the same app token; stop it |
| `missing_scope` from a helper | add the scope in the app config and reinstall the app |
