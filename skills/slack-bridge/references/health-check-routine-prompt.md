# Health-check routine prompt (scheduled)

Paste this into a **scheduled** Grok Bot routine (for example "Slack bridge
health check") in the owner's main agent. One health check is enough for the
whole install: every route (main conversation and dedicated channel agents)
goes through the same bridge process, so dedicated agents do not need their
own health check.

## Schedule

Default: **hourly during waking hours**, e.g. 08:00–24:00 in the owner's
time zone:

```cron
CRON_TZ=Asia/Shanghai 4 8-23 * * *
```

(runs at 08:04, 09:04, … 23:04; the odd minute avoids the busy top of the
hour). Every run is a full agent turn and costs usage, so do not schedule it
more often than you need: 30-minute checks roughly double the cost and, on a
box that is frozen when idle, mostly add false alarms (see below). Night
runs are only worth it if someone relies on the bot at night. On a host with
plain cron, `ensure-running.sh` costs nothing and can run every 10 minutes
([operations.md](../../../docs/operations.md#plain-cron-alternative)).

## Prompt

Replace `/workspace/slack-bot` with your install directory.

```text
Slack bridge health check. On the box, run
/workspace/slack-bot/scripts/ensure-running.sh --quiet
and note its output and exit code.

Background: the box is frozen when nobody uses it and is resumed by routine
runs like this one. Right after a resume the heartbeat can look stale or the
socket disconnected; the bridge normally reconnects by itself within ~30 s.
So a restart (action=restarted / action=started) followed by a healthy status
is a known false alarm.

- Exit 0, no output, or action=started / action=restarted: run
  /workspace/slack-bot/scripts/status.sh once more. If it is healthy
  (running, heartbeat fresh, connected), finish silently. Do NOT notify the
  owner about the restart.
- Exit 3 (another check in progress): finish silently.
- Only hand off to the main conversation (one short message) when:
  - the bridge is still unhealthy after the restart, or
  - the script fails: exit 1 (action=failed) or 2 (action=blocked), or the
    command itself was blocked, or
  - something new and actionable shows up (for example a token error).
  In that case also run /workspace/slack-bot/scripts/doctor.sh and
  /workspace/slack-bot/scripts/status.sh 30, find the matching row in
  /workspace/grok-slack-bridge/docs/operations.md ("Common failures and
  fixes"), and say what failed and the exact fix the owner needs to do
  (for example which secret to update).

Never print, copy or store token, webhook URL or Authorization values.
Do not post anything in Slack.
```
