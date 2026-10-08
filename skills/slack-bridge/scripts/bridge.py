#!/usr/bin/env python3
"""Slack Socket Mode -> agent webhook bridge.

Pipeline for every Slack message event:

  ack -> normalize -> persist receipt (SQLite, dedup by event_id and
  channel:ts, fingerprint conflicts rejected) -> decide (forward / ignore)
  -> queue -> delivery worker -> webhook -> accepted | unknown-result | failed

The agent replies with reply.sh, which marks the operation completed (or
no_reply). Nothing is ever resent blindly: a timeout after sending is
"unknown-result", and operations that were in flight when the bridge stopped
become "needs-reconciliation" on the next start.

Agent-view events (`app_context_changed`, `agent_session_stopped`,
`agent_session_title_changed`, `app_home_opened`) are handled locally.

Secrets come only from environment variables and are never logged.
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import logging
import re
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import access  # noqa: E402
import common  # noqa: E402
import events  # noqa: E402
import outbox as outboxmod  # noqa: E402
import store as storemod  # noqa: E402
import webhook  # noqa: E402

log = logging.getLogger("slack-bridge")


class Decision:
    def __init__(self, action: str, reason: str = "", entry: str = "", reply_text: str = "",
                 verdict: access.Verdict | None = None, command: str = "", not_before: float = 0,
                 reactivate: bool = False):
        self.command = command
        self.not_before = not_before
        self.reactivate = reactivate
        self.action = action        # forward | ignore | command
        self.reason = reason
        self.entry = entry          # dm | mention | thread_follow | channel | slash_command
        self.reply_text = reply_text
        self.verdict = verdict

    def __repr__(self) -> str:  # pragma: no cover
        return f"Decision({self.action}, {self.reason!r}, {self.entry})"


class Bridge:
    def __init__(self, home: Path, dry_run: bool = False, web=None, poster=None, now=time.time,
                 secrets: dict | None = None, sync_outbox: bool = False):
        self.home = Path(home)
        self.public_home = self.home  # path shown in reply commands
        self.dry_run = dry_run
        self.now = now
        self.cfg = common.load_config(self.home)
        self.store = storemod.open_store(self.home)
        self.store.now = now
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evt")
        self.user_cache: dict[str, dict] = {}
        self.stats = {"received": 0, "forwarded": 0, "skipped": 0, "failed": 0}
        self.started_at = time.time()
        self.last_connected_at = self.started_at
        self.was_connected = False
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        # Secrets come from take_secrets() (removed from os.environ so child
        # processes never inherit them) or, in tests/dry runs, from the env.
        self.secrets = secrets if secrets is not None else {
            n: common.env_value(n) for n in common.REQUIRED_ENV}
        self.webhook_url = self.secrets.get(common.ENV_WEBHOOK_URL, "")
        self.webhook_auth = common.normalize_auth_header(self.secrets.get(common.ENV_WEBHOOK_AUTH, ""))
        self.outbox = outboxmod.Outbox(
            self._post_now, max_items=int(self.cfg.get("outbox_max_items", 50)),
            min_interval=float(self.cfg.get("outbox_min_interval_seconds", 1.0)), sync=sync_outbox)
        self.disconnect_reported = False
        self.poster = poster or webhook.post_json
        self.web = web
        self.socket = None
        self.ident = events.Identity(team_id=self.cfg.get("team_id", ""), app_id=self.cfg.get("app_id", ""),
                                     bot_user_id=self.cfg.get("bot_user_id", ""))
        self.agent_dedupe = common.Deduper()
        self.viewing: dict[str, dict] = {}
        self.sessions_unavailable_logged = False
        self.delivery_thread: threading.Thread | None = None
        self.denied_at: dict[str, float] = {}

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient

        self.web = WebClient(token=self.secrets.get(common.ENV_BOT_TOKEN, ""))
        auth = self.web.auth_test()
        self.ident.bot_user_id = auth.get("user_id", "")
        self.ident.bot_id = auth.get("bot_id", "")
        self.ident.team_id = auth.get("team_id", "")
        try:
            self.ident.app_id = (self.web.bots_info(bot=self.ident.bot_id).get("bot") or {}).get("app_id", "")
        except Exception as exc:
            log.warning("bots.info failed (%s); app id from config: %s",
                        common.slack_error_code(exc), self.cfg.get("app_id") or "(unknown)")
            self.ident.app_id = self.cfg.get("app_id", "")
        self._remember_identity(auth)
        log.info("authenticated as @%s (%s) app=%s in %s (%s)", auth.get("user"),
                 self.ident.bot_user_id, self.ident.app_id or "?", auth.get("team"), auth.get("team_id"))

        self.socket = SocketModeClient(
            app_token=self.secrets.get(common.ENV_APP_TOKEN, ""),
            web_client=self.web,
            auto_reconnect_enabled=True,
        )
        self.socket.socket_mode_request_listeners.append(self.on_request)
        self.socket.connect()
        log.info("socket mode connected; webhook host=%s; owner=%s",
                 common.webhook_host(self.webhook_url), self.cfg.get("owner_user_id") or "(unset)")

    def _remember_identity(self, auth) -> None:
        """Record non-secret identity facts in config.json for status/doctor."""
        updates = {
            "bot_user_id": auth.get("user_id", ""),
            "team_id": auth.get("team_id", ""),
            "workspace": auth.get("team", ""),
            "workspace_url": auth.get("url", ""),
            "app_id": self.ident.app_id,
        }
        updates = {k: v for k, v in updates.items() if v and self.cfg.get(k) != v}
        if updates:
            try:
                on_disk = common.load_config(self.home)
                on_disk.update(updates)
                common.save_config(self.home, on_disk)
                self.cfg.update(updates)
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("could not update config.json: %s", exc)

    def start_workers(self) -> None:
        """Recover persisted state and start the delivery worker."""
        recovered = self.store.recover_after_restart()
        paused = self.store.pause_bot_threads("bridge restarted; bot conversations wait for a human")
        if paused:
            log.info("paused %d thread(s) with bot activity until a human continues them", paused)
        if recovered:
            log.warning("%d operation(s) were in flight when the bridge stopped; marked "
                        "needs-reconciliation (not replayed): %s", len(recovered),
                        ", ".join(r["op_id"] for r in recovered[:10]))
        for row in self.store.undecided():
            # Recorded but never decided (crash between receipt and decision):
            # nothing was sent yet, so deciding now is a first attempt, not a replay.
            try:
                envelope = json.loads(row["envelope"] or "null")
                msg, _ = events.normalize(envelope, self.ident, catchup=bool(envelope.get("catchup")))
            except Exception:
                msg = None
            if msg is None:
                self.store.transition(row["op_id"], "ignored", "undecided receipt without event data")
                continue
            log.info("deciding receipt %s left in 'received' before the restart", row["op_id"])
            self.process(msg, row["thread_key"])
        problems = access.validate(self.cfg)
        for problem in problems:
            log.warning("config: %s", problem)
        self.outbox.start()
        counts = self.store.counts()
        self.report("startup", (
            f"{self.cfg.get('bot_name') or 'Bridge'} bridge (re)started (pid {os.getpid()}). "
            f"In flight before the restart: {len(recovered)} (marked needs-reconciliation, not replayed); "
            f"bot threads paused: {paused}; waiting for a decision: "
            f"{counts.get('needs-reconciliation', 0) + counts.get('unknown-result', 0)}"
            + (f"; config warnings: {len(problems)}" if problems else "") + "."), force=True)
        self.on_startup(recovered)
        self.delivery_thread = threading.Thread(target=self.delivery_loop, name="delivery", daemon=True)
        self.delivery_thread.start()

    def on_startup(self, recovered: list[dict]) -> None:
        """Hook for later stages (loop control, monitoring)."""

    def write_heartbeat(self) -> None:
        now = time.time()
        connected = bool(self.socket and self.socket.is_connected())
        if connected:
            self.last_connected_at = now
        state = {
            "pid": os.getpid(),
            "started_at": int(self.started_at),
            "heartbeat_at": int(now),
            "connected": connected,
            "last_connected_at": int(self.last_connected_at),
            "bot_user_id": self.ident.bot_user_id,
            "app_id": self.ident.app_id,
            "webhook_host": common.webhook_host(self.webhook_url),
            **self.stats,
            "operations": self.store.counts(),
        }
        run = self.home / "run"
        run.mkdir(parents=True, exist_ok=True)
        tmp = run / ".heartbeat.json.tmp"
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, run / "heartbeat.json")
        if connected and not self.was_connected:
            self.pool.submit(self.catch_up_safe)
            if self.disconnect_reported:
                self.disconnect_reported = False
                self.report("reconnected", "Bridge reconnected to Slack; catching up on followed threads.",
                            force=True)
        limit = float(self.cfg.get("report_disconnect_seconds", 300))
        if not connected and not self.disconnect_reported and now - self.last_connected_at > limit:
            self.disconnect_reported = True
            self.report("disconnected", f"Bridge has been disconnected from Slack for "
                        f"{int(now - self.last_connected_at)}s (auto-reconnect is retrying).")
        self.was_connected = connected

    def serve_forever(self) -> None:
        last_prune = 0.0
        while not self.stop_event.is_set():
            try:
                self.write_heartbeat()
                if time.time() - last_prune > 86400:
                    last_prune = time.time()
                    pruned = self.store.prune(float(self.cfg.get("retention_days", 30)))
                    if pruned:
                        log.info("pruned %d old receipts", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("heartbeat failed: %s", exc)
            self.stop_event.wait(30)

    def shutdown(self, *_args) -> None:
        log.info("shutting down")
        self.stop_event.set()
        self.wake.set()
        self.outbox.stop()
        try:
            if self.socket:
                self.socket.close()
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)

    # -- inbound -------------------------------------------------------------
    def on_request(self, client, req) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        # Ack first, always, so Slack does not retry. The receipt is persisted
        # next; a crash between ack and receipt is the only loss window, and
        # thread catch-up covers followed threads.
        if req.type == "slash_commands":
            # Decide synchronously so the ack can carry the (ephemeral) answer.
            try:
                text = self.handle_slash(req.payload)
            except Exception:
                log.exception("slash command failed")
                text = self.cfg.get("error_text") or common.DEFAULT_CONFIG["error_text"]
            client.send_socket_mode_response(SocketModeResponse(
                envelope_id=req.envelope_id, payload={"response_type": "ephemeral", "text": text}))
            return
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type == "events_api":
            self.pool.submit(self.handle_envelope, req.payload)
        elif req.type == "interactive":
            self.pool.submit(self.handle_interactive_safe, req.payload)
        else:
            log.debug("ignoring socket request type %s", req.type)

    def handle_envelope(self, envelope: dict) -> None:
        try:
            self._handle_envelope(envelope)
        except Exception:
            self.stats["failed"] += 1
            log.exception("unhandled error processing event %s", envelope.get("event_id"))

    def _handle_envelope(self, envelope: dict) -> str:
        """Process one envelope; returns a short outcome string (for tests/logs)."""
        event = envelope.get("event") or {}
        self.stats["received"] += 1
        if event.get("type") in common.AGENT_EVENTS:
            self.handle_agent_event(envelope, event)
            return "agent-event"
        self.cfg = common.load_config(self.home)  # config edits apply live
        msg, why = events.normalize(envelope, self.ident, catchup=bool(envelope.get("catchup")))
        if msg is None:
            self.stats["skipped"] += 1
            log.debug("noise %s: %s", envelope.get("event_id"), why)
            return f"noise:{why}"
        tkey = msg.thread_key(self.ident)
        status, row = self.store.record(
            msg.op_id, msg.fingerprint, msg_key=msg.msg_key, kind=msg.event_type,
            team_id=msg.team_id, channel=msg.channel, ts=msg.ts, thread_key=tkey,
            actor=msg.actor, actor_type=msg.actor_type, envelope=envelope)
        if status == "duplicate":
            self.stats["skipped"] += 1
            log.debug("duplicate %s (already %s as %s)", msg.op_id, row["state"], row["op_id"])
            return "duplicate"
        if status == "conflict":
            self.stats["skipped"] += 1
            log.warning("rejected %s: same id/message as %s with different content",
                        msg.op_id, row["op_id"])
            return "conflict"
        return self.process(msg, tkey)

    def process(self, msg: events.Msg, tkey: str) -> str:
        """Decide and act on a message whose receipt is in state 'received'."""
        thread = self.store.ensure_thread(tkey, msg.team_id or self.ident.team_id, msg.channel,
                                          msg.root_ts, self.ident.app_id)
        log.info("event %s %s channel=%s actor=%s(%s) ts=%s len=%d%s%s",
                 msg.op_id, msg.event_type, msg.channel, msg.actor, msg.actor_type, msg.ts,
                 len(msg.text), " catchup" if msg.catchup else "",
                 f" text={msg.text!r}" if self.cfg.get("log_message_text") else "")
        decision = self.decide(msg, thread)
        return self.apply_decision(msg, thread, decision)

    def entry_for(self, msg: events.Msg, thread: dict, policy: dict) -> str | None:
        """How this message addresses the bot (None = it does not)."""
        if msg.event_type == "slash_command":
            return "slash_command"
        if msg.is_dm:
            return "dm"
        if msg.mentions_bot:
            return "mention"
        in_thread = bool(msg.thread_ts) and msg.thread_ts != msg.ts
        if policy["trigger"] in ("thread_follow", "all") and in_thread and thread.get("following"):
            return "thread_follow"
        if policy["trigger"] == "all":
            return "channel"
        return None

    COMMAND_STRIP = " \t\n!！.。,，?？~～:：-"

    def command_of(self, msg: events.Msg, thread: dict, entry: str | None,
                   verdict: access.Verdict) -> str:
        """stop / new / resume as a bare text command from a human (never from bots)."""
        if msg.actor_type != "human":
            return ""
        in_followed_thread = bool(msg.thread_ts) and bool(thread.get("following"))
        if entry is None and not in_followed_thread:
            return ""
        text = re.sub(r"<@[A-Z0-9]+(\|[^>]*)?>", " ", msg.text or "")
        text = " ".join(text.split()).strip(self.COMMAND_STRIP).lower()
        if not text or len(text) > 20:
            return ""
        words = dict(common.DEFAULT_CONFIG["command_words"])
        words.update(self.cfg.get("command_words") or {})
        for cmd in ("stop", "new", "resume", "help", "status"):
            if text in {str(w).lower() for w in words.get(cmd, [])}:
                if cmd in ("new", "resume", "status") and verdict.role != "owner":
                    return ""  # just a normal message from a non-owner
                if cmd in ("resume", "help", "status") and entry is None:
                    return ""
                return cmd
        return ""

    def decide(self, msg: events.Msg, thread: dict) -> Decision:
        """Access first (same check for every entry point), then commands, task state,
        trigger and bot loop limits."""
        policy = access.effective_policy(self.cfg, msg.channel)
        entry = self.entry_for(msg, thread, policy)
        verdict = access.check(self.cfg, self.ident, msg.actor_obj(), channel=msg.channel,
                               root_ts=msg.root_ts, entry=entry or "channel")
        if not verdict.allowed:
            return Decision("ignore", f"refused: {verdict.reason}", verdict=verdict,
                            reply_text=self.deny_text(msg, entry))
        cmd = self.command_of(msg, thread, entry, verdict)
        if cmd:
            return Decision("command", f"command: {cmd}", entry=entry or "thread_follow",
                            verdict=verdict, command=cmd)
        if entry is None:
            return Decision("ignore", f"trigger={policy['trigger']}: not addressed to the bot",
                            verdict=verdict)
        state = thread.get("state") or "active"
        reactivate = False
        if state in ("stopped", "paused"):
            if msg.actor_type == "bot":
                return Decision("ignore", f"thread {state}: bots wait until a human continues it",
                                verdict=verdict)
            if entry not in ("dm", "mention", "slash_command"):
                return Decision("ignore", f"thread {state}: mention the bot to continue", verdict=verdict)
            reactivate = True
        not_before = 0.0
        if msg.actor_type == "bot":
            limit = int(policy["max_bot_turns"])
            if verdict.bot_entry and verdict.bot_entry.get("max_turns") not in (None, ""):
                limit = min(limit, int(verdict.bot_entry["max_turns"]))
            turns = int(thread.get("bot_turns") or 0)
            if turns >= limit:
                return Decision("ignore", f"max_bot_turns reached ({turns}/{limit}); the owner can "
                                "say 'new' to start a new task", verdict=verdict)
            last = float(thread.get("last_bot_at") or 0)
            cooldown = float(policy["bot_cooldown_seconds"])
            if last and self.now() - last < cooldown:
                not_before = last + cooldown
        return Decision("forward", verdict.reason, entry=entry, verdict=verdict,
                        not_before=not_before, reactivate=reactivate)

    def command_targets(self, msg: events.Msg, thread: dict, cmd: str) -> list[dict]:
        """The thread a command is about; top-level commands cover the channel's open work."""
        if msg.thread_ts and msg.thread_ts != msg.ts:
            return [thread]
        if cmd == "stop":
            return [t for t in self.store.channel_threads(msg.channel)
                    if t["state"] != "stopped" and self.store.open_ops(t["thread_key"])] + [thread]
        return [t for t in self.store.channel_threads(msg.channel, ("stopped", "paused"))] + [thread]

    def run_command(self, msg: events.Msg, thread: dict, decision: Decision) -> str:
        cmd = decision.command
        self.store.transition(msg.op_id, "ignored", decision.reason)
        targets = {} if cmd in ("help", "status") else \
            {t["thread_key"]: t for t in self.command_targets(msg, thread, cmd)}
        log.info("command %s by %s in %s covers %d thread(s)", cmd, msg.actor, msg.channel, len(targets))
        if cmd == "stop":
            stopped = []
            for key, t in targets.items():
                stopped += self.store.stop_thread(key, f"stop from {msg.actor}")
                if t.get("root_ts"):
                    common.mark_stopped(self.home, t["channel"], t["root_ts"])
            log.info("stopped operations: %s", ", ".join(stopped) or "none")
            text = self.cfg.get("stop_message")
        elif cmd == "help":
            text = self.cfg.get("help_text") or common.DEFAULT_CONFIG["help_text"]
        elif cmd == "status":
            text = self.status_text(thread if msg.thread_ts else None)
        elif cmd == "new":
            for key in targets:
                self.store.new_task(key, f"new task from {msg.actor}")
            text = self.cfg.get("new_task_message")
        else:
            for key in targets:
                self.store.update_thread(key, state="active", state_reason=f"resumed by {msg.actor}")
            text = self.cfg.get("resume_message")
        if text and not self.dry_run:
            self.notify(msg.channel, self.reply_thread(msg), text)
        return f"command:{cmd}"

    def deny_text(self, msg: events.Msg, entry: str | None) -> str:
        """Fixed refusal text: humans only, DMs/commands only, at most once per hour per user."""
        if msg.actor_type != "human" or entry not in ("dm", "slash_command"):
            return ""
        text = self.cfg.get("deny_message") or ""
        last = self.denied_at.get(msg.user, 0)
        if not text or time.time() - last < 3600:
            return ""
        self.denied_at[msg.user] = time.time()
        return text

    def apply_decision(self, msg: events.Msg, thread: dict, decision: Decision) -> str:
        if decision.action == "command":
            return self.run_command(msg, thread, decision)
        if decision.action != "forward":
            self.stats["skipped"] += 1
            self.store.transition(msg.op_id, "ignored", decision.reason)
            log.info("ignored %s: %s", msg.op_id, decision.reason)
            if decision.reply_text and not self.dry_run:
                self.notify(msg.channel, self.reply_thread(msg), decision.reply_text)
            return f"ignored:{decision.reason}"
        updates = {"following": 1}
        if decision.reactivate:
            updates.update(state="active", state_reason=f"continued by {msg.actor} ({decision.entry})")
        thread = self.store.update_thread(thread["thread_key"], **updates) or thread
        if msg.actor_type == "bot":
            thread = self.store.add_bot_turn(thread["thread_key"]) or thread
        session = self.open_session(msg)
        reaction = self.cfg.get("ack_reaction") if self.cfg.get("react_on_receipt") else ""
        if reaction and msg.ts and not session and not self.dry_run and self.web is not None:
            self._safe(self.web.reactions_add, channel=msg.channel, timestamp=msg.ts, name=reaction)
        payload = common.build_payload(
            msg, self.cfg, self.public_home, entry=decision.entry, thread=thread,
            user_info=self.user_info(msg.user), session=session,
            viewing=self.viewing.get(msg.user),
            permissions=decision.verdict.permissions if decision.verdict else None)
        if self.dry_run:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        self.store.transition(msg.op_id, "queued", decision.reason, payload=payload,
                              task_id=thread.get("task_id"), not_before=decision.not_before or None)
        self.wake.set()
        return "queued"

    # -- slash commands and buttons (same access check) ---------------------
    def slash_actor(self, payload: dict) -> access.Actor:
        return access.Actor(user=payload.get("user_id") or "", team_id=str(payload.get("team_id") or ""),
                            api_app_id=str(payload.get("api_app_id") or ""), is_bot=False)

    def handle_slash(self, payload: dict) -> str:
        """Returns the ephemeral text shown to the invoking user."""
        self.cfg = common.load_config(self.home)
        actor = self.slash_actor(payload)
        verdict = access.check(self.cfg, self.ident, actor, channel=payload.get("channel_id") or "",
                               entry="slash_command")
        log.info("slash command %s by %s in %s: %s", payload.get("command"), actor.user,
                 payload.get("channel_id"), "allowed" if verdict.allowed else verdict.reason)
        if not verdict.allowed:
            return self.cfg.get("deny_message") or "Not allowed."
        text = (payload.get("text") or "").strip()
        if not text:
            return self.cfg.get("slash_usage_text") or common.DEFAULT_CONFIG["slash_usage_text"]
        dm = self.dm_channel(actor.user)
        if not dm:
            return self.cfg.get("error_text") or common.DEFAULT_CONFIG["error_text"]
        msg = events.slash_message(payload, dm)
        self.pool.submit(self._record_and_process, msg, {"slash": payload})
        return self.cfg.get("slash_ack_text") or common.DEFAULT_CONFIG["slash_ack_text"]

    def dm_channel(self, user: str) -> str:
        if self.web is None:
            return "D-" + user
        try:
            return (self.web.conversations_open(users=user).get("channel") or {}).get("id", "")
        except Exception as exc:
            log.warning("conversations.open failed: %s", common.slack_error_code(exc))
            return ""

    def _record_and_process(self, msg: events.Msg, envelope: dict) -> str:
        tkey = msg.thread_key(self.ident)
        status, row = self.store.record(msg.op_id, msg.fingerprint, msg_key=msg.msg_key,
                                        kind=msg.event_type, team_id=msg.team_id, channel=msg.channel,
                                        ts=msg.ts, thread_key=tkey, actor=msg.actor,
                                        actor_type=msg.actor_type, envelope=None)
        if status != "new":
            return status
        return self.process(msg, tkey)

    def handle_interactive_safe(self, payload: dict) -> None:
        try:
            self.handle_interactive(payload)
        except Exception:
            log.exception("interactive payload failed")

    def handle_interactive(self, payload: dict) -> list[str]:
        """Buttons: every action goes through the same access check."""
        self.cfg = common.load_config(self.home)
        if payload.get("type") != "block_actions":
            return []
        user = payload.get("user") or {}
        team = (payload.get("team") or {}).get("id") or user.get("team_id") or ""
        actor = access.Actor(user=user.get("id") or "", team_id=team,
                             user_team=user.get("team_id") or "",
                             api_app_id=str(payload.get("api_app_id") or ""), is_bot=False)
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id", "")
        message = payload.get("message") or {}
        root = message.get("thread_ts") or message.get("ts") or ""
        results = []
        for action in payload.get("actions") or []:
            action_id = action.get("action_id") or ""
            verdict = access.check(self.cfg, self.ident, actor, channel=channel, root_ts=root, entry="button")
            if not verdict.allowed:
                log.info("button %s refused for %s: %s", action_id, actor.user, verdict.reason)
                results.append("refused")
                continue
            results.append(self.run_button(action_id, action.get("value") or "", actor, channel, root, verdict))
        return results

    def run_button(self, action_id: str, value: str, actor: access.Actor, channel: str, root: str,
                   verdict: access.Verdict) -> str:
        log.info("button %s by %s (no handler)", action_id, actor.user)
        return "unsupported"

    def reply_thread(self, msg: events.Msg) -> str | None:
        return common.reply_target(msg.raw, bool(self.cfg.get("dm_reply_in_thread")))["thread_ts"]

    # -- delivery ------------------------------------------------------------
    def delivery_loop(self) -> None:
        while not self.stop_event.is_set():
            op = self.store.next_queued()
            if op is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            try:
                self.deliver(op)
            except Exception:
                log.exception("delivery of %s crashed", op["op_id"])
                time.sleep(1)

    def deliver(self, op: dict) -> str:
        """Deliver one queued operation; returns its new state."""
        if self.dry_run:
            return op["state"]
        hold = self.before_submit(op)
        if hold:
            self.store.transition(op["op_id"], hold[0], hold[1])
            log.info("not delivering %s: %s", op["op_id"], hold[1])
            return hold[0]
        payload = json.loads(op["payload"] or "{}")
        self.store.transition(op["op_id"], "submitted", "posting to webhook", attempts_inc=1)
        result = self.poster(self.webhook_url, self.webhook_auth, payload,
                             float(self.cfg.get("webhook_timeout_seconds", 20)))
        attempts = op["attempts"] + 1
        if result.outcome == "accepted":
            self.store.transition(op["op_id"], "accepted", f"HTTP {result.status}")
            thread = self.store.thread(op["thread_key"]) if op.get("thread_key") else None
            if thread and thread["state"] == "stopped":  # stopped while in flight
                self.store.transition(op["op_id"], "stopped", "thread stopped while submitting")
            self.stats["forwarded"] += 1
            log.info("forwarded %s -> HTTP %s", op["op_id"], result.status)
            self.on_delivery_result(op, "accepted", result)
            return "accepted"
        if result.outcome == "unknown":
            self.store.transition(op["op_id"], "unknown-result", result.detail)
            log.error("delivery of %s has an unknown result (%s); not resending. Resolve with "
                      "slackctl.sh ops resolve/retry.", op["op_id"], result.detail)
            self.on_delivery_result(op, "unknown-result", result)
            return "unknown-result"
        retries = max(1, int(self.cfg.get("webhook_retries", 3)))
        if result.outcome == "retry" and attempts < retries:
            delay = 3 ** (attempts - 1)
            self.store.transition(op["op_id"], "queued", f"retry after {result.detail}",
                                  not_before=time.time() + delay)
            log.warning("webhook not reached for %s (%s); retry %d/%d in %ss", op["op_id"],
                        result.detail, attempts, retries, delay)
            self.on_delivery_result(op, "queued", result)
            return "queued"
        self.store.transition(op["op_id"], "failed", result.detail)
        self.stats["failed"] += 1
        if result.outcome == "rejected":
            log.error("webhook rejected %s (%s); the webhook URL or Authorization value is "
                      "probably stale. Run scripts/doctor.sh and scripts/reconfigure.sh.",
                      op["op_id"], result.detail)
        else:
            log.error("giving up on %s after %d attempts (%s)", op["op_id"], attempts, result.detail)
        self.on_delivery_result(op, "failed", result)
        return "failed"

    def before_submit(self, op: dict) -> tuple[str, str] | None:
        """Last check before sending: stop wins. None = go ahead."""
        thread = self.store.thread(op["thread_key"]) if op.get("thread_key") else None
        if thread and thread["state"] == "stopped":
            return ("stopped", f"thread stopped before submit ({thread.get('state_reason') or 'stop'})")
        if thread and thread["state"] == "paused" and op.get("actor_type") == "bot":
            return ("ignored", f"thread paused before submit ({thread.get('state_reason') or ''})")
        return None

    def on_delivery_result(self, op: dict, state: str, result) -> None:
        if state == "accepted":
            self.store.reset_counter("webhook_failures")
            return
        n = self.store.bump("webhook_failures")
        threshold = int(self.cfg.get("report_webhook_failures", 3))
        if threshold and n >= threshold:
            status = f" HTTP {result.status}" if getattr(result, "status", 0) else ""
            self.report("webhook", f"Webhook delivery problems: {n} in a row (last: {result.outcome}"
                        f"{status}). Messages are kept in the queue; run scripts/doctor.sh. "
                        "Stale URL/Authorization: scripts/reconfigure.sh.")
        if state in ("failed", "unknown-result") and self.web is not None:
            payload = json.loads(op["payload"] or "{}")
            session = payload.get("agent_session")
            if session:
                self.set_session_status(session["channel"], session["thread_ts"], "active")
            err = self.cfg.get("error_reaction")
            if err and op.get("ts"):
                self._safe(self.web.reactions_add, channel=op["channel"], timestamp=op["ts"], name=err)
            if state == "failed" and op.get("actor_type") == "human":
                reply = payload.get("reply") or {}
                text = self.cfg.get("error_text") or common.DEFAULT_CONFIG["error_text"]
                self.notify(reply.get("channel") or op["channel"], reply.get("thread_ts"), text)

    # -- monitoring ------------------------------------------------------------
    def report(self, key: str, text: str, force: bool = False) -> bool:
        """Operational report to report_channel/report_thread_ts (rate limited per key)."""
        log.info("report[%s]: %s", key, text)
        channel = self.cfg.get("report_channel") or ""
        if not channel or self.dry_run or self.web is None:
            return False
        interval = float(self.cfg.get("report_min_interval_seconds", 900))
        last = float(self.store.meta(f"report:{key}") or 0)
        if not force and self.now() - last < interval:
            return False
        self.store.meta(f"report:{key}", str(self.now()))
        self.outbox.put(channel, self.cfg.get("report_thread_ts") or None, text)
        return True

    def status_text(self, thread: dict | None) -> str:
        counts = self.store.counts()
        connected = bool(self.socket and self.socket.is_connected()) if self.socket else None
        up = int((time.time() - self.started_at) / 60)
        lines = [f"Bridge: pid {os.getpid()}, up {up} min, Slack connected: "
                 f"{'yes' if connected else 'no' if connected is False else 'n/a'}.",
                 f"Operations: {counts.get('queued', 0)} queued, {counts.get('accepted', 0)} with the agent, "
                 f"{counts.get('needs-reconciliation', 0) + counts.get('unknown-result', 0)} need a decision, "
                 f"{counts.get('failed', 0)} failed."]
        if thread:
            policy = access.effective_policy(self.cfg, thread["channel"])
            lines.append(f"This thread: {thread['state']}, task #{thread['task_id']}, bot turns "
                         f"{thread['bot_turns']}/{policy['max_bot_turns']}, trigger {policy['trigger']}.")
        return "\n".join(lines)

    # -- catch-up --------------------------------------------------------------
    def catch_up_safe(self) -> None:
        try:
            self.catch_up()
        except Exception:
            log.exception("thread catch-up failed")

    def catch_up(self) -> dict:
        """Re-read followed threads past their cursor after (re)connecting.

        A failed or partial read marks the thread catchup_ok=0 and leaves the
        cursor where it was; it never counts as "no new messages".
        """
        summary = {"threads": 0, "messages": 0, "failed": 0}
        if self.web is None or not self.cfg.get("catchup_enabled", True):
            return summary
        window = float(self.cfg.get("catchup_window_hours", 24)) * 3600
        for t in self.store.following_threads(time.time() - window):
            summary["threads"] += 1
            oldest = t["cursor_ts"] or t["root_ts"]
            cursor, msgs, ok = None, [], True
            for _page in range(20):
                try:
                    kwargs = {"channel": t["channel"], "ts": t["root_ts"], "oldest": oldest,
                              "limit": 200, "inclusive": False}
                    if cursor:
                        kwargs["cursor"] = cursor
                    resp = self.web.conversations_replies(**kwargs)
                except Exception as exc:
                    log.warning("catch-up read failed for %s: %s", t["thread_key"],
                                common.slack_error_code(exc))
                    ok = False
                    break
                msgs.extend(resp.get("messages") or [])
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not resp.get("has_more") or not cursor:
                    break
            else:
                ok = False  # too many pages: treat as incomplete
            if not ok:
                summary["failed"] += 1
                self.store.update_thread(t["thread_key"], catchup_ok=0)
                continue
            ctype = "im" if t["channel"].startswith("D") else "channel"
            for m in sorted(msgs, key=lambda m: storemod._ts_key(m.get("ts"))):
                if storemod._ts_key(m.get("ts")) <= storemod._ts_key(oldest):
                    continue
                env = events.catchup_envelope(m, t["channel"], ctype, self.ident)
                outcome = self._handle_envelope(env)
                if outcome not in ("duplicate",) and not outcome.startswith("noise"):
                    summary["messages"] += 1
            self.store.update_thread(t["thread_key"], catchup_ok=1)
        if summary["messages"] or summary["failed"]:
            log.info("catch-up: %s", summary)
        return summary

    # -- agent sessions (Slack agent_view) --------------------------------
    def slack_api(self, method: str, body: dict) -> dict:
        from slack_sdk.errors import SlackApiError
        try:
            return dict(self.web.api_call(method, json=body).data)
        except SlackApiError as exc:
            return dict(getattr(exc.response, "data", None) or {"ok": False, "error": str(exc)})

    def set_session_status(self, channel: str, thread_ts: str, status: str, **extra) -> dict:
        body = {"channel_id": channel, "thread_ts": thread_ts, "status": status}
        body.update({k: v for k, v in extra.items() if v})
        try:
            return self.slack_api("agents.sessions.setStatus", body)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_session(self, msg: events.Msg) -> dict | None:
        """Put the message's thread into an agent session in "processing"."""
        if not self.cfg.get("agent_sessions", True) or self.dry_run or self.web is None:
            return None
        if msg.actor_type != "human":
            return None
        thread_ts = msg.root_ts
        if not thread_ts:
            return None
        title = "" if msg.thread_ts else common.session_title(msg.text, self.cfg.get("session_title_chars", 60))
        resp = self.set_session_status(msg.channel, thread_ts, "processing",
                                       title=title, initiator_user_id=msg.user)
        if not resp.get("ok"):
            level = log.debug if self.sessions_unavailable_logged else log.info
            level("agent session unavailable (%s); using reaction fallback.", resp.get("error"))
            self.sessions_unavailable_logged = True
            return None
        self.sessions_unavailable_logged = False
        log.info("agent session processing channel=%s thread=%s", msg.channel, thread_ts)
        return {"channel": msg.channel, "thread_ts": thread_ts, "status": "processing"}

    def handle_agent_event(self, envelope: dict, event: dict) -> None:
        etype = event.get("type")
        if self.agent_dedupe.seen(envelope.get("event_id", "")):
            return
        self.cfg = common.load_config(self.home)
        if etype == "app_context_changed":
            users = [a.get("user_id") for a in envelope.get("authorizations") or []
                     if a.get("user_id") and not a.get("is_bot")]
            user = event.get("user") or (users[0] if users else "")
            channels = common.context_channels(event)
            if user:
                self.viewing[user] = {"channel_ids": channels, "updated_at": int(time.time())}
            log.info("context changed user=%s channels=%s", user or "?", channels)
        elif etype == "agent_session_stopped":
            self.on_session_stopped(event, envelope)
        elif etype == "app_home_opened":
            log.debug("app home opened user=%s tab=%s", event.get("user"), event.get("tab"))
        elif etype == "agent_session_title_changed":
            log.info("session renamed channel=%s thread=%s", event.get("channel"), event.get("thread_ts"))

    def on_session_stopped(self, event: dict, envelope: dict | None = None) -> None:
        channel, thread_ts = event.get("channel"), event.get("thread_ts")
        log.info("session stop requested channel=%s thread=%s by %s", channel, thread_ts, event.get("user"))
        if not channel or not thread_ts:
            return
        envelope = envelope or {}
        actor = access.Actor(user=event.get("user") or "", team_id=str(envelope.get("team_id") or ""),
                             api_app_id=str(envelope.get("api_app_id") or ""))
        verdict = access.check(self.cfg, self.ident, actor, channel=channel, root_ts=thread_ts, entry="button")
        if not verdict.allowed:
            log.info("session stop refused: %s", verdict.reason)
            return
        key = storemod.thread_key(self.ident.team_id, channel, thread_ts, self.ident.app_id)
        self.store.ensure_thread(key, self.ident.team_id, channel, thread_ts, self.ident.app_id)
        self.store.stop_thread(key, f"agent view Stop by {actor.user}")
        if self.dry_run:
            return
        self.set_session_status(channel, thread_ts, "active")
        msg = self.cfg.get("stop_message")
        if msg:
            self.notify(channel, thread_ts, msg)
        common.mark_stopped(self.home, channel, thread_ts)

    # -- outbound helpers ------------------------------------------------------
    def notify(self, channel: str, thread_ts: str | None, text: str) -> None:
        """Bridge-originated message (fixed texts only)."""
        if self.web is None or self.dry_run or not text:
            return
        self.outbox.put(channel, thread_ts, text, allowed_users=[self.cfg.get("owner_user_id") or ""])

    def _post_now(self, msg: dict) -> None:
        if self.web is not None:
            self.web.chat_postMessage(**msg)

    def user_info(self, user: str) -> dict:
        if not user or self.web is None:
            return {}
        if user in self.user_cache:
            return self.user_cache[user]
        info = {}
        try:
            resp = self.web.users_info(user=user)
            u = resp.get("user") or {}
            profile = u.get("profile") or {}
            info = {"name": profile.get("display_name") or u.get("name", ""),
                    "real_name": profile.get("real_name") or u.get("real_name", "")}
        except Exception as exc:
            log.warning("users.info failed for %s: %s", user, common.slack_error_code(exc))
        self.user_cache[user] = info
        return info

    def _safe(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except Exception as exc:
            code = common.slack_error_code(exc)
            if code in ("already_reacted", "no_reaction"):
                return None
            log.warning("%s failed: %s", getattr(fn, "__name__", "slack call"), code)
            return None


def acquire_instance_lock(home: Path):
    """Hold an exclusive flock on run/bridge.lock for the life of the process."""
    import fcntl
    path = home / "run" / "bridge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", help="install directory (default: $SLACK_BRIDGE_HOME or scripts/..)")
    parser.add_argument("--dry-run-event", metavar="JSON_FILE",
                        help="decide and build the webhook payload for an Events API envelope and "
                             "print it, using a throwaway state database; no network, no secrets")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    home = common.resolve_home(args.home)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("slack_sdk", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.dry_run_event:
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="slack-bridge-dry-"))
        try:
            if common.config_path(home).exists():
                shutil.copy(common.config_path(home), tmp / "config.json")
            bridge = Bridge(tmp, dry_run=True)
            bridge.public_home = home
            bridge.ident.app_id = bridge.cfg.get("app_id", "")
            with open(args.dry_run_event, encoding="utf-8") as fh:
                envelope = json.load(fh)
            print("outcome:", bridge._handle_envelope(envelope), file=sys.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0

    problems = common.check_env()
    if problems:
        print("slack-bridge: cannot start, environment is incomplete:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Provide the missing values as environment variables (never in files). "
              "See README.md -> 'Secrets'.", file=sys.stderr)
        return 2

    if importlib.util.find_spec("slack_sdk") is None:
        print("slack-bridge: slack_sdk is not installed; run scripts/install.sh", file=sys.stderr)
        return 3

    lock = acquire_instance_lock(home)
    if lock is None:
        print(f"slack-bridge: another bridge is already running for {home} "
              "(run/bridge.lock is held); not starting a second one", file=sys.stderr)
        return 5
    secrets = common.take_secrets()

    pidfile = home / "run" / "bridge.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def _cleanup_pidfile() -> None:
        try:
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass

    atexit.register(_cleanup_pidfile)

    bridge = Bridge(home, secrets=secrets)
    bridge.instance_lock = lock
    signal.signal(signal.SIGTERM, bridge.shutdown)
    signal.signal(signal.SIGINT, bridge.shutdown)
    try:
        bridge.connect()
    except Exception as exc:
        log.error("startup failed: %s", common.slack_error_code(exc))
        return 4
    bridge.start_workers()
    bridge.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
