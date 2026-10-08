# Session model: which conversation handles a Slack message

## Each Slack message starts a routine run

The bridge forwards every accepted Slack message (DM, @mention, followed
thread, `/grok`) as **one webhook POST**, and each POST starts **its own
routine run**. A run does not see what earlier runs did. If runs answered
Slack themselves, every message would be handled by a fresh agent with no
memory of the previous message, and two quick messages could be answered by
two runs racing each other.

## Main-session handoff: one shared, ordered context

So the routine run is only a dispatcher. The payload carries
`routing` (built by the bridge from `config.json`):

```json
"routing": {"target": "main", "busy_policy": "interrupt_merge",
            "source": "default", "label": "", "webhook": "default"}
```

- `target: "main"` — the run hands the whole payload to the owner's **main
  Grok Bot conversation** and ends. All DMs and every channel without its
  own agent land in that one conversation, so it keeps one shared, ordered
  context (what was asked in the DM earlier, which task is in progress, what
  was already answered), and it answers with the payload's
  `reply.command` (`reply.sh --op …`), which records the operation as
  `completed` in the bridge's state.
- `target: "dedicated"` — the channel is routed to a separate Grok Bot
  agent with its own webhook; that agent's conversation handles the
  channel's messages with its own context.

`source` is `default` (no entry for this channel) or `channel`; `webhook`
says which webhook the bridge used; `fallback` appears when a dedicated
route could not be used (its env variables were missing), in which case the
message went to the default webhook and should be handled as `main`.

The bridge's own delivery guarantees are unchanged by routing: receipts and
dedup, per-thread order, busy-retry ("排队中…", 20/40/80/160 s then every
5 min up to 15 min), no blind resend after a timeout or HTTP 504, and
`stop`/`停` cancelling queued work all work the same for every target.

## When the handling conversation is busy: interrupt vs queue

A message can arrive while the main (or dedicated) conversation is still
working on an earlier one. `busy_policy` (global, overridable per channel)
tells it what to do:

| Policy | Behaviour | Good for | Cost |
| --- | --- | --- | --- |
| `interrupt_merge` (default) | Pause at a safe point. A follow-up in the **same** thread/DM ("also do X", "no, use Y") is merged into the unfinished work, and one combined answer goes out with the newest operation's `reply.command`; the earlier merged operations are closed with `no_reply_command --reason "merged into Ev…"`. A message from another thread is its own task: answer it, then resume. | Conversational use: corrections and additions take effect immediately; no stale answer to a question the user already changed. | Long tasks get interrupted; partial work must be reconciled; more care needed so every operation is closed. |
| `queue` | Finish the current task, then take new messages in arrival order. | Long, independent jobs; channels where messages are separate requests. | A correction waits until the old (now wrong) task is done; replies can feel slow. |

Either way, **every `operation_id` gets exactly one `reply.sh --op …`** —
a reply or `--no-reply` — so `slackctl.sh ops list` shows nothing left open.

This is a policy for the agent conversation; the bridge just passes it
through. (The bridge-level busy-retry is a different thing: it covers the
webhook endpoint refusing a POST, e.g. HTTP 409/429 while a run is still
starting.)

## Configuration

```json
"session_routing": {
  "default": "main",
  "channels": {
    "C0123456789": {
      "target": "dedicated",
      "label": "release channel bot",
      "webhook_url_env": "GROK_WEBHOOK_URL_RELEASE",
      "webhook_auth_env": "GROK_WEBHOOK_AUTH_RELEASE",
      "busy_policy": "queue"
    }
  }
},
"busy_policy": "interrupt_merge"
```

- `default` must be `main` (a dedicated agent is always per channel).
- A channel entry may also say `"target": "main"` just to override
  `busy_policy` for that channel.
- `webhook_url_env` / `webhook_auth_env` are environment variable
  **names**. The values (URL and `Authorization` header) are secrets and
  never go in `config.json`; `config set`/`save_config` refuse values that
  look like URLs or tokens in these fields.
- Unconfigured channels and DMs always use the default webhook
  (`GROK_WEBHOOK_URL` / `GROK_WEBHOOK_AUTH`).

## Adding a dedicated per-channel bot

1. Create the dedicated Grok Bot agent and its webhook routine; get its
   URL and `Authorization` value.
2. Choose two env names that are not the default ones, e.g.
   `GROK_WEBHOOK_URL_RELEASE` / `GROK_WEBHOOK_AUTH_RELEASE`, and add the
   channel entry above to `config.json` (`slackctl.sh config set
   session_routing '<json>'`, or edit the file).
3. Export both variables in the shell that starts the bridge, then
   `scripts/restart.sh` (the bridge reads secrets only at start and removes
   them from its environment). Config changes alone are picked up live, but
   a new env variable needs the restart.
4. Check: `slackctl.sh routing --channel C0123456789` (effective route, env
   names only), `scripts/doctor.sh` (env present + TLS reachability of the
   dedicated host, no request sent), `status.sh` / `run/heartbeat.json`
   (`dedicated_routes` lists the channel and webhook host).
5. If the variables are missing at start, the bridge logs a warning and
   delivers that channel to the default webhook with
   `routing.fallback` set — messages are never dropped because of routing.

To remove a dedicated route, delete the channel entry; the channel goes back
to the main conversation immediately.
