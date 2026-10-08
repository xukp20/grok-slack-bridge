# Conversation guidelines: top-level conversations dispatch, they don't block

This applies to every conversation that answers Slack through slack-bridge
at the top level: the owner's **main conversation** (DMs and channels
without their own agent, reached through the silent handoff) and each
**dedicated channel agent** (see [dedicated-channel-bot.md](dedicated-channel-bot.md)).
The pasteable instruction block is
[skills/slack-bridge/references/dispatcher-guideline.md](../skills/slack-bridge/references/dispatcher-guideline.md).

## Why: messages arrive only between turns

```
Slack ─▶ bridge (👀, queue) ─▶ webhook ─▶ routine run ─▶ handoff ─▶ main conversation
                                                                  (delivered when its
                                                                   current turn ends)
```

- The bridge forwards each message within a second and shows 👀, so the
  user knows it arrived.
- The routine run hands the message to the main conversation, but the
  handoff is **delivered only after the conversation's current turn
  ends**. A conversation that is busy for ten minutes leaves every new
  Slack message waiting for those ten minutes.
- A blocking wait inside a turn (a `sleep 60`, a "wait for 60 s and see if
  anything comes in", a polling loop) is **not interrupted** by a new
  message. We tested this: a follow-up sent during a 60-second wait was
  not seen until the turn ended and the queue was checked by hand.
- Messages typed directly into the agent app can reach a busy conversation
  sooner than Slack messages, because they don't go through the webhook
  and handoff. Don't rely on Slack behaving the same way.

So `busy_policy: interrupt_merge` can only take effect at a turn boundary.
The way to make Slack feel responsive is to make turns short.

## The rules

1. **Plan and dispatch; don't execute long work in the top-level turn.**
   Quick answers and a few short commands are fine. Anything longer goes
   to a background task with a self-contained brief: goal, context, the
   files and commands involved, constraints (no pushing, no messages to
   anyone, secrets rules), done criteria and what to report.
2. **Keep turns short**: well under two minutes of tool time as a rule of
   thumb.
3. **Never block**: no sleeps, waits or polling loops in the top-level
   conversation. End the turn instead; the background task's completion or
   the next message starts the next turn.
4. **Say what happens next.** For dispatched work, an interim reply with
   `--session-status processing` keeps the operation open and the 👀 in
   place; the final `reply.command` closes it after you've checked the
   result. A short task can just be answered.
5. **Check for newer messages before the final reply.**
   `slackctl.sh ops list --state accepted --state queued` lists operations
   handed over but not answered yet (and ones still waiting to be posted);
   or re-read the thread with `slackctl.sh thread`. Apply `busy_policy`:
   `interrupt_merge` folds same-thread follow-ups into one answer sent with
   the newest operation's `reply.command` and closes the older ones with
   `no_reply_command --reason "merged into <op>"`; `queue` answers in
   order. Other threads are separate tasks.
6. **Exactly one final `reply.sh --op` per operation** (answer or
   `--no-reply`), so `ops list` shows nothing left open.
7. **Decisions and outgoing messages stay at the top.** Background tasks
   do the work and report back; approvals, messages to other people and
   anything irreversible are decided in the top-level conversation.
8. **Verify before relaying**, and redirect or stop a background task when
   the user changes the request.

## Examples

| Request | Do | Don't |
| --- | --- | --- |
| "What does `busy_policy` do?" | Answer in this turn. | Start a background task. |
| "Add feature X, run the tests, commit locally" | Interim reply ("started; I'll report when tests pass"), dispatch a background task with the brief, end the turn. Final reply after checking its report. | Edit and run the suite for 10 minutes in the top-level turn. |
| "Wait a minute, then I'll send a correction" | Reply that you're ready and end the turn; the correction arrives as the next message. | `sleep 60` inside the turn. |
| A follow-up "also do Y" arrives while a background task runs | In the next (short) turn, redirect the task or queue Y; reply once per `busy_policy`. | Ignore it until the task ends. |
| Two quick messages in one thread | Check `ops list` before replying; answer both in one reply, close the older operation as merged. | Two answers, the first already stale. |

## Dedicated channel agents

A dedicated agent answers in its routine run, and every message starts its
own run, so one long run does not hold the next message back the same way.
The rules still apply: overlapping runs can be refused by the platform
(the bridge then queues with "排队中…" and retries slowly), the user still
waits for the long run's answer, and two runs can answer the same thread.
So keep runs short, re-read the thread before answering, and close a
message another run already answered with `no_reply_command`. See
[dedicated-routine-prompt.md](../skills/slack-bridge/references/dedicated-routine-prompt.md).
