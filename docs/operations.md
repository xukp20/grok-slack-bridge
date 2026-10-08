# Operations: restarts, crashes, recovery

Paths below assume the install directory `/workspace/slack-bot`; replace it
with yours (`$SLACK_BRIDGE_HOME`).

## What happens when…

| Event | Effect | Recovery |
| --- | --- | --- |
| **Machine/box restarts** | The bridge process is gone. `run/bridge.pid` may remain but is ignored once the PID is dead. Slack shows the bot as online (`always_online`) but nobody receives events. | `start.sh` (or let `ensure-running.sh` do it) |
| **Bridge crashes or is killed** | Same as above; the last lines of `logs/bridge.log` usually show why. | `start.sh` |
| **Network blip / Slack refreshes the socket** | `slack_sdk` reconnects automatically; the heartbeat may briefly show `connected=false`. | None needed |
| **Process alive but hung** | `run/heartbeat.json` stops updating (it is rewritten every 30 s). | `restart.sh` / `ensure-running.sh` |
| **Agent shell or chat session ends** | No effect: `start.sh` detaches with `setsid nohup`. | None |

Messages sent while no bridge is connected are **not** delivered later
(Slack only retries Socket Mode events briefly). After reconnecting, the
bridge reads threads it follows (`catchup_window_hours`) and processes each
missed message once; DMs and new top-level mentions are not caught up. After
an outage, ask people to resend, or read what was missed with
`scripts/slackctl.sh thread --channel <D…|C…>` (recent history).

**Each Slack message starts a separate routine run.** Runs overlap when
someone writes again while the agent is still working; a routine that does
not accept a parallel run answers with an error status (HTTP 400 was seen).
The bridge treats 400/408/409/425/429 and 5xx other than 504 as "busy": the operation stays
`queued`, is retried slowly in thread order, and the user sees "排队中…"
instead of an error (see the troubleshooting table below).

**Restarts never replay work.** Operations that were `submitted` or
`accepted` when the bridge stopped become `needs-reconciliation`; a webhook
timeout is `unknown-result`. Neither is resent automatically, because the
agent may already be working on it. Decide per operation:

```bash
S=/workspace/slack-bot/scripts/slackctl.sh
$S ops list --state needs-reconciliation --state unknown-result
$S ops show Ev0123                          # history of state transitions
$S ops resolve Ev0123 --to completed        # the agent answered (or --to no_reply / ignored)
$S ops retry Ev0123 --force                 # the agent never got it: queue it once more
```

A late `reply.sh --op Ev0123` from the agent also completes it. Threads that
had bot traffic are `paused` after a restart; bots stay ignored there until
a human mentions the bot (or the owner says `resume`/`new`).

## Check

```bash
/workspace/slack-bot/scripts/status.sh [N]      # process health (pid, heartbeat, connected) and task state, last N log lines
/workspace/slack-bot/scripts/slackctl.sh health --json   # the same, machine-readable
/workspace/slack-bot/scripts/doctor.sh          # env vars, tokens, webhook TLS, access config, process, lock, task state
/workspace/slack-bot/scripts/doctor.sh --ping-webhook   # also sends one test POST (wakes the agent once)
/workspace/slack-bot/scripts/ensure-running.sh --dry-run  # what self-healing would do, without doing it
```

Files:

| Path | Contents |
| --- | --- |
| `logs/bridge.log` | bridge log (rotated to `bridge.log.1` above 5 MB at start); metadata only, no message text by default |
| `logs/ensure-running.log` | one line per `ensure-running.sh` run (`action=none/started/restarted/blocked/failed`) |
| `run/bridge.pid` | PID of the running bridge |
| `run/bridge.lock` | held (flock) by the running bridge; a second bridge for the same install refuses to start |
| `run/heartbeat.json` | `heartbeat_at`, `connected`, `last_connected_at`, event counters, operations by state |
| `run/bridge.sqlite` | receipts, operation state transitions, thread task state (pruned after `retention_days`) |

**Process health and task state are separate.** `status.sh` exits 0 when
the process runs, even if some operations need a decision; those are listed
under "tasks" and flagged by `doctor.sh` as warnings, never fixed by a
restart.

### Reports in Slack

Set `report_channel` (and optionally `report_thread_ts`) and invite the bot
there to get, rate limited per kind (`report_min_interval_seconds`):
bridge (re)starts with a recovery summary, `report_webhook_failures`
consecutive delivery problems, and Slack disconnects longer than
`report_disconnect_seconds` (plus the reconnect). Empty = log only.

## Restart

```bash
/workspace/slack-bot/scripts/start.sh      # idempotent: no-op if already running
/workspace/slack-bot/scripts/restart.sh    # stop + start; needed to pick up changed env vars
/workspace/slack-bot/scripts/stop.sh
```

`start.sh`/`restart.sh` need all four variables (`SLACK_BOT_TOKEN`,
`SLACK_APP_TOKEN`, `GROK_WEBHOOK_URL`, `GROK_WEBHOOK_AUTH`) in the
environment of the shell that runs them; they refuse to start and list what
is missing otherwise. The bridge keeps the values it started with until it
is restarted, and removes them from its own environment after reading them
(processes it might spawn never inherit them).

On a **Grok Bot box**, secrets saved for the agent are injected into new
processes automatically, so any agent shell (or a routine run) can simply
call `start.sh`. On other machines, load them from your secret manager into
the shell first; never put them in a file in the install directory.

## Common failures and fixes

| Symptom (log / doctor) | Cause | Fix |
| --- | --- | --- |
| `startup failed: invalid_auth`; doctor `bot token (auth.test) FAIL invalid_auth` | Bot token rotated/revoked or app reinstalled | Copy the new `xoxb-` token (OAuth & Permissions), update the secret, `reconfigure.sh` |
| doctor `app token … invalid_auth` / `not_allowed_token_type` | App-level token revoked, or a bot token was given as `SLACK_APP_TOKEN` | Generate an `xapp-` token with `connections:write`, update, `reconfigure.sh` |
| Only some messages get 👀 / answers; counters lower than expected | **Two bridges** connected with the same app token (old machine, old account, a second install). Slack splits events across connections. | Stop the other one (`stop.sh` there); check with `pgrep -af bridge.py` on every machine you used |
| 👀 then ⚠️ and the fixed error text; log `webhook rejected … HTTP 401/403` (Authorization) or `404/410` (URL) | Routine webhook URL or key rotated, routine deleted, or agent switched | Copy the new URL and Authorization from the routine, update `GROK_WEBHOOK_*`, `reconfigure.sh --ping-webhook` |
| Log `webhook busy for … (HTTP 400/409/429/5xx except 504); retry n in Ns`; agent view shows "排队中…" (or ⏳ on the message) | **Overlapping runs.** Each Slack message starts its own routine run; while the previous run is still working, the routine can reject the new one (seen as HTTP 400). A `doctor.sh --ping-webhook` right after usually returns 200. | Nothing: the message stays queued and is retried after 20s, 40s, 80s, 160s, then every 5 min, for up to 15 min (`webhook_busy_*`), behind earlier messages of the same thread/DM. `stop`/`停` cancels it. Only if it still fails does the user get "请稍后重新发送" and the operation becomes `failed` |
| Log `webhook stayed busy for … giving up` | The routine was busy (or erroring) for the whole retry window | Ask whether the previous task is stuck; check the routine's runs; the user resends. Raise `webhook_busy_max_seconds` for long tasks |
| ⚠️ with `giving up on … after 3 attempts (not delivered: …)` | Webhook host unreachable (connect/TLS failure, request never sent) | `doctor.sh` (TLS check); retry later |
| `delivery of … has an unknown result (timed out … / HTTP 504 …)`; op `unknown-result` | No answer after the request was sent, or a gateway timeout (504) after forwarding it: the agent may be running it | Never resent automatically. `ops show`, then `ops resolve --to completed` if the agent answered, or `ops retry --force` if it clearly did not; raise `webhook_timeout_seconds` |
| Repeated `slack_sdk.socket_mode` warnings/errors, heartbeat `connected=false` for minutes | Socket disconnect loop: network trouble, app token revoked mid-run, or too many connections (max 10 per app) | `doctor.sh`; fix token; stop stray bridges; `restart.sh`. `ensure-running.sh` restarts after `--stale-seconds` (default 300) of disconnection |
| No 👀 at all in a channel | Bot not in the channel, or message did not @mention it | `/invite @Grok Bot`; mention it |
| No 👀 at all in DMs, status says stopped | Machine restarted / crash | `start.sh` |
| `slack_sdk is not installed` / missing venv | Install directory moved or venv deleted | `skills/slack-bridge/scripts/install.sh <dir>` (safe to re-run) |

## Self-healing: `ensure-running.sh`

`scripts/ensure-running.sh` is safe to run as often as you like:

- running, heartbeat fresh, socket connected → does nothing (`action=none`, exit 0)
- not running → `start.sh` (`action=started`)
- heartbeat older than `--stale-seconds` (default 300), or socket disconnected
  for longer than that → `restart.sh` (`action=restarted`)
- the four env vars are missing → changes nothing, `action=blocked`, exit 2
- recovery attempted but failed → `action=failed`, exit 1
- another run in progress (flock) → exit 3

Options: `--dry-run` (report only), `--quiet` (print only when something
happened), `--stale-seconds N`. Every run appends one line to
`logs/ensure-running.log`.

### Suggested Grok Bot routine

Create a scheduled routine, e.g. **hourly, Monday–Friday** (add a run at
09:00 on weekends if you want), with this prompt:

> Slack bridge health check. On the box, run
> `/workspace/slack-bot/scripts/ensure-running.sh --quiet`.
> - No output, or a line with `action=started` / `action=restarted`: the bridge
>   is fine (or was recovered). Finish without notifying me, except mention a
>   restart in one short sentence.
> - Exit code 1 or 2 (`action=failed` / `action=blocked`): run
>   `/workspace/slack-bot/scripts/doctor.sh` and
>   `/workspace/slack-bot/scripts/status.sh 30`, look up the matching row in
>   `/workspace/grok-slack-bridge/docs/operations.md` ("Common failures and
>   fixes"), and tell me what failed and the exact fix I need to do (for
>   example, which secret to update).
> Never print, copy or store token values, and do not post anything in Slack.

The routine's shell receives the box secrets, so `ensure-running.sh` can
start the bridge without extra setup.

### Plain cron alternative

On a normal Linux host with cron, and with the four variables available to
cron jobs (for example via your secret manager's wrapper; do not write
them into the crontab or a file in the install directory):

```cron
*/10 * * * * /workspace/slack-bot/scripts/ensure-running.sh --quiet >/dev/null 2>&1
@reboot sleep 30 && /workspace/slack-bot/scripts/ensure-running.sh --quiet >/dev/null 2>&1
```

Without the variables, cron runs end with `action=blocked` in
`logs/ensure-running.log` and change nothing. (The Grok Bot box has no
cron daemon by default; use the routine there.)
