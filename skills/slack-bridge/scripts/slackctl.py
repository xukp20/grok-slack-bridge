#!/usr/bin/env python3
"""slackctl: reply, react, inspect, configure, and diagnose the Slack bridge.

Secrets are read from environment variables only:
  SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-),
  GROK_WEBHOOK_URL, GROK_WEBHOOK_AUTH (full Authorization header value).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import common  # noqa: E402
import outbox as outboxmod  # noqa: E402

BOT_SCOPES = [
    "app_mentions:read",
    "canvases:read",
    "canvases:write",
    "channels:history",
    "channels:read",
    "chat:write",
    "commands",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "reactions:read",
    "reactions:write",
    "users.profile:read",
    "users:read",
    "users:read.email",
]
# Channel/group-DM messages and reactions are subscribed so the bridge can later
# follow threads the bot is in without an @mention; until it filters them,
# common.should_handle drops them (only DMs and @mentions are forwarded).
BOT_EVENTS = [
    "app_mention",
    "message.im",
    "message.channels",
    "message.groups",
    "message.mpim",
    "reaction_added",
]
DEFAULT_SLASH_COMMAND = "/grok"
# Slack agent features ("Agents" in app settings / manifest features.agent_view).
AGENT_SCOPES = ["assistant:write"]
AGENT_EVENTS = ["app_home_opened", "app_context_changed", "agent_session_stopped", "agent_session_title_changed"]
DEFAULT_PROMPTS = [
    {"title": "What can you do?", "message": "What can you help me with here in Slack?"},
    {"title": "Summarize this channel",
     "message": "Summarize the recent discussion in the channel I'm looking at."},
    {"title": "Draft a reply", "message": "Help me draft a reply to the latest message."},
]
SESSION_STATUSES = ("processing", "active", "suspended", "closed")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 2) -> None:
    print(f"slackctl: {msg}", file=sys.stderr)
    sys.exit(code)


def web_client():
    problems = common.check_env([common.ENV_BOT_TOKEN])
    if problems:
        die(problems[0] + " — export it in the environment (never write it to a file).")
    try:
        from slack_sdk import WebClient
    except ImportError:
        die("slack_sdk is not installed; run scripts/install.sh (or use the venv python)", 3)
    return WebClient(token=common.env_value(common.ENV_BOT_TOKEN))


def slack_error(exc: Exception) -> str:
    data = getattr(getattr(exc, "response", None), "data", None)
    if isinstance(data, dict):
        extra = data.get("needed") and f" (needed scope: {data['needed']})" or ""
        return f"{data.get('error')}{extra}"
    return str(exc)


def read_text(args) -> str:
    if args.text is not None:
        return args.text
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        die("no text: pass --text, --text-file, or pipe the message on stdin")
    return sys.stdin.read()


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def build_manifest(name: str, description: str, color: str = "#111827",
                   display_name: str | None = None, agent_view: bool = True,
                   prompts: list[dict] | None = None,
                   slash_command: str | None = DEFAULT_SLASH_COMMAND) -> dict:
    manifest = {
        "display_information": {
            "name": name,
            "description": description,
            "background_color": color,
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": display_name or name,
                "always_online": True,
            },
        },
        "oauth_config": {"scopes": {"bot": list(BOT_SCOPES)}},
        "settings": {
            "event_subscriptions": {"bot_events": list(BOT_EVENTS)},
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }
    if slash_command:
        cmd = slash_command if slash_command.startswith("/") else "/" + slash_command
        manifest["features"]["slash_commands"] = [{
            "command": cmd,
            "description": f"Ask {name}"[:100],
            "usage_hint": "[question or task]",
            "should_escape": False,
        }]
    else:
        manifest["oauth_config"]["scopes"]["bot"].remove("commands")
    if agent_view:
        manifest["features"]["app_home"]["agent_tasks_enabled"] = True
        manifest["features"]["agent_view"] = {
            "agent_description": description[:300],
            "suggested_prompts": list(DEFAULT_PROMPTS if prompts is None else prompts)[:4],
        }
        manifest["oauth_config"]["scopes"]["bot"] = sorted(
            manifest["oauth_config"]["scopes"]["bot"] + AGENT_SCOPES)
        manifest["settings"]["event_subscriptions"]["bot_events"] = BOT_EVENTS + AGENT_EVENTS
    return manifest


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    plain = (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ .:/()'-]*", text)
             and ": " not in text and not text.endswith((":", " "))
             and text.lower() not in ("true", "false", "yes", "no", "on", "off", "null", "~"))
    return text if plain else json.dumps(text, ensure_ascii=False)


def to_yaml(data, indent: int = 0) -> str:
    pad = "  " * indent
    lines = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.append(to_yaml(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item:
                first = True
                for key, value in item.items():
                    lead = "- " if first else "  "
                    lines.append(f"{pad}{lead}{key}: {_yaml_scalar(value)}")
                    first = False
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return "\n".join(lines)


def parse_prompts(values: list[str] | None) -> list[dict] | None:
    if not values:
        return None
    prompts = []
    for v in values:
        title, sep, message = v.partition("|")
        if not sep or not title.strip() or not message.strip():
            die(f"--prompt expects 'Title|Message', got {v!r}")
        prompts.append({"title": title.strip(), "message": message.strip()})
    if len(prompts) > 4:
        die("Slack allows at most 4 suggested prompts")
    return prompts


def cmd_render_manifest(args) -> int:
    manifest = build_manifest(args.name, args.description, args.color, args.display_name,
                              agent_view=not args.no_agent_view,
                              prompts=parse_prompts(args.prompt),
                              slash_command=None if args.slash_command in ("", "none")
                              else args.slash_command)
    if args.format == "json":
        text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    else:
        text = to_yaml(manifest) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


# ---------------------------------------------------------------------------
# messaging
# ---------------------------------------------------------------------------

def post_reply(client, channel: str, text: str, thread_ts: str | None, fmt: str) -> list[str]:
    sent = []
    chunks = common.chunk_text(text)
    for chunk in chunks:
        kwargs = {"channel": channel, "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if fmt == "markdown":
            try:
                resp = client.chat_postMessage(
                    text=common.markdown_to_mrkdwn(chunk)[:3900],
                    blocks=[{"type": "markdown", "text": chunk}], **kwargs)
                sent.append(resp["ts"])
                continue
            except Exception as exc:
                if "invalid_blocks" not in slack_error(exc):
                    raise
                # Older workspaces without markdown blocks: fall back to mrkdwn.
                chunk = common.markdown_to_mrkdwn(chunk)
        elif fmt == "mrkdwn":
            chunk = common.markdown_to_mrkdwn(chunk)
        resp = client.chat_postMessage(text=chunk, mrkdwn=(fmt != "plain"), **kwargs)
        sent.append(resp["ts"])
    return sent


def api_json(client, method: str, body: dict) -> dict:
    try:
        return dict(client.api_call(method, json=body).data)
    except Exception as exc:
        data = getattr(getattr(exc, "response", None), "data", None)
        return dict(data) if isinstance(data, dict) else {"ok": False, "error": str(exc)}


def recently_stopped(home: Path, channel: str, thread_ts: str, window: int = 3600) -> bool:
    try:
        data = json.loads((home / "run" / "stopped_sessions.json").read_text())
    except Exception:
        return False
    at = data.get(f"{channel}:{thread_ts}")
    return bool(at) and time.time() - float(at) < window


def set_session(client, home: Path, channel: str, thread_ts: str | None, status: str) -> dict | None:
    """Set an agent session status; quietly no-op when there is no session."""
    if not status or status == "none" or not thread_ts:
        return None
    if status == "processing" and recently_stopped(home, channel, thread_ts):
        print("slackctl: note: the user pressed Stop in this thread; leaving it active",
              file=sys.stderr)
        status = "active"
    resp = api_json(client, "agents.sessions.setStatus",
                    {"channel_id": channel, "thread_ts": thread_ts, "status": status})
    if not resp.get("ok"):
        print(f"slackctl: note: agents.sessions.setStatus({status}) failed: {resp.get('error')}",
              file=sys.stderr)
    return resp


def open_store_quiet(home: Path):
    try:
        import store as storemod
        return storemod.open_store(home)
    except Exception as exc:  # the reply must still work without state
        print(f"slackctl: note: state database unavailable ({type(exc).__name__})", file=sys.stderr)
        return None


def finish_op(st, op_id: str, state: str, reason: str) -> str:
    """Move an operation to completed/no_reply when it is still open."""
    if st is None or not op_id:
        return "untracked"
    row = st.get(op_id)
    if row is None:
        print(f"slackctl: note: unknown operation {op_id}", file=sys.stderr)
        return "unknown"
    if row["state"] in ("accepted", "needs-reconciliation", "unknown-result"):
        st.transition(op_id, state, reason)
        thread = st.thread(row["thread_key"]) if row.get("thread_key") else None
        if thread and thread["state"] in ("active", "completed", "no_reply"):
            st.update_thread(row["thread_key"], state=state, state_reason=reason)
        return state
    return row["state"]


def mention_allowed(cfg: dict, st, op_id: str | None, extra) -> set[str]:
    """Who a reply may ping: owner, the requester, allow-listed bots, mention_allowlist."""
    allowed = {cfg.get("owner_user_id") or ""}
    allowed |= set(cfg.get("mention_allowlist") or [])
    allowed |= {str(e.get("user_id")) for e in cfg.get("bot_allowlist") or []
                if isinstance(e, dict) and e.get("user_id")}
    for item in extra or []:
        allowed |= {v.strip().strip("<@>") for v in item.replace(",", " ").split()}
    if st is not None and op_id:
        row = st.get(op_id)
        if row and row.get("actor"):
            allowed.add(row["actor"])
    return {a for a in allowed if a}


def stop_gate(st, op_id: str | None, channel: str, thread_ts: str | None) -> str | None:
    """Why a reply must not be sent (operation or thread stopped), else None."""
    if st is None:
        return None
    if op_id:
        row = st.get(op_id)
        if row and row["state"] == "stopped":
            return f"operation {op_id} was stopped ({row.get('reason') or 'stop'})"
        if row and row.get("thread_key"):
            t = st.thread(row["thread_key"])
            if t and t["state"] == "stopped":
                return f"thread was stopped ({t.get('state_reason') or 'stop'})"
    if thread_ts:
        t = st.find_thread(channel, thread_ts)
        if t and t["state"] == "stopped":
            return f"thread was stopped ({t.get('state_reason') or 'stop'})"
    return None


def clear_ack(client, cfg: dict, channel: str, ack_ts: str | None, ack_reaction: str | None,
              done_reaction: str | None, st=None, op_id: str | None = None) -> str | None:
    """Undo the bridge's receipt ack after the final reply / --no-reply (best effort).

    With --op the bridge recorded what it did (reaction or assistant status) in the
    state database; otherwise fall back to removing the configured reaction from
    --ack-ts. Returns what was cleared ("reaction", "status") or None."""
    info = None
    if st is not None and op_id:
        try:
            raw = st.meta_pop(f"ack:{op_id}")
            info = json.loads(raw) if raw else None
        except Exception:
            info = None
    cleared = None
    if info and info.get("kind") == "status":
        resp = api_json(client, "assistant.threads.setStatus",
                        {"channel_id": info.get("channel") or channel,
                         "thread_ts": info.get("thread_ts"), "status": ""})
        cleared = "status" if resp.get("ok") else None
    else:
        target_ts = (info or {}).get("ts") or ack_ts
        if target_ts and (info or ack_ts):
            name = (info or {}).get("name") or ack_reaction or common.ack_settings(cfg)["emoji"]
            try:
                client.reactions_remove(channel=(info or {}).get("channel") or channel,
                                        timestamp=target_ts, name=name.strip(":"))
                cleared = "reaction"
            except Exception as exc:
                if slack_error(exc) not in ("no_reaction",):
                    print(f"slackctl: note: could not remove :{name}: ({slack_error(exc)})",
                          file=sys.stderr)
    if done_reaction and ack_ts:
        try:
            client.reactions_add(channel=channel, timestamp=ack_ts, name=done_reaction.strip(":"))
        except Exception:
            pass
    return cleared


def cmd_reply(args) -> int:
    home = common.resolve_home(args.home)
    cfg = common.load_config(home)
    st = open_store_quiet(home)
    gate = None if args.force else stop_gate(st, args.op, args.channel, args.thread_ts)
    if gate and not args.no_reply:
        # Stop wins: never post into a stopped task (use --force for a manual message).
        print(json.dumps({"ok": False, "sent": False, "stopped": True, "reason": gate,
                          "operation": args.op}))
        return 3
    if args.no_reply:
        client = web_client()
        state = finish_op(st, args.op, "no_reply", args.reason or "agent chose not to reply") \
            if not gate else "stopped"
        session = set_session(client, home, args.channel, args.thread_ts, args.session_status or "active") \
            if (args.session_status or args.thread_ts) else None
        clear_ack(client, cfg, args.channel, args.ack_ts, args.ack_reaction, None, st, args.op)
        out = {"ok": True, "sent": False, "operation": args.op, "operation_state": state}
        if session is not None:
            out["session_status"] = "active" if session.get("ok") else f"error: {session.get('error')}"
        print(json.dumps(out))
        return 0
    text = read_text(args).strip()
    if not text:
        die("refusing to send an empty message (use --no-reply to record that you chose not to answer)")
    text = outboxmod.sanitize(text, mention_allowed(cfg, st, args.op, args.allow_mention))
    client = web_client()
    try:
        sent = post_reply(client, args.channel, text, args.thread_ts, args.format)
    except Exception as exc:
        if args.session_status and args.session_status != "none":
            set_session(client, home, args.channel, args.thread_ts, "active")
        die(f"chat.postMessage failed: {slack_error(exc)}", 1)
    session = set_session(client, home, args.channel, args.thread_ts, args.session_status or "none")
    state = "untracked"
    if args.op:
        state = "accepted (interim)" if args.session_status == "processing" else \
            finish_op(st, args.op, "completed", f"replied ts={sent[-1] if sent else ''}")
    if args.session_status != "processing":  # an interim update keeps the receipt ack
        clear_ack(client, cfg, args.channel, args.ack_ts, args.ack_reaction, args.done_reaction,
                  st, args.op)
    out = {"ok": True, "channel": args.channel, "thread_ts": args.thread_ts, "ts": sent,
           "operation": args.op, "operation_state": state}
    if session is not None:
        out["session_status"] = session.get("agent_status") or session.get("status") \
            if session.get("ok") else f"error: {session.get('error')}"
    print(json.dumps(out))
    return 0


def cmd_upload(args) -> int:
    """Upload a local file into a channel/thread (needs files:write)."""
    home = common.resolve_home(args.home)
    cfg = common.load_config(home)
    path = Path(args.file)
    if not path.is_file():
        die(f"no such file: {path}")
    st = open_store_quiet(home)
    gate = None if args.force else stop_gate(st, args.op, args.channel, args.thread_ts)
    if gate:
        print(json.dumps({"ok": False, "sent": False, "stopped": True, "reason": gate}))
        return 3
    client = web_client()
    kwargs = {"channel": args.channel, "file": str(path), "filename": args.filename or path.name,
              "title": args.title or path.name}
    if args.thread_ts:
        kwargs["thread_ts"] = args.thread_ts
    if args.comment:
        kwargs["initial_comment"] = outboxmod.sanitize(args.comment, mention_allowed(cfg, st, args.op, None))
    try:
        resp = client.files_upload_v2(**kwargs)
    except Exception as exc:
        err = slack_error(exc)
        hint = {"missing_scope": " (add files:write, then reinstall the app)",
                "not_in_channel": " (/invite the bot to the channel first)"}.get(err, "")
        die(f"upload failed: {err}{hint}", 1)
    f = resp.get("file") or (resp.get("files") or [{}])[0]
    print(json.dumps({"ok": True, "file_id": f.get("id"), "name": f.get("name"),
                      "permalink": f.get("permalink")}))
    return 0


def cmd_download(args) -> int:
    """Download a Slack file (by F… id or url_private) with the bot token."""
    client = web_client()
    url = args.url
    name = None
    if args.file_id:
        try:
            info = client.files_info(file=args.file_id).get("file") or {}
        except Exception as exc:
            err = slack_error(exc)
            hint = {"missing_scope": " (add files:read, then reinstall the app)",
                    "file_not_found": " (wrong id, or the file is not shared where the bot is)"}.get(err, "")
            die(f"files.info failed: {err}{hint}", 1)
        url = info.get("url_private_download") or info.get("url_private")
        name = info.get("name")
    if not url:
        die("give --file-id F… or --url <url_private>")
    if not urllib.parse.urlparse(url).hostname or \
            not urllib.parse.urlparse(url).hostname.endswith(SLACK_FILE_HOSTS):
        die("refusing to send the bot token to a non-Slack host")
    out = Path(args.out or name or Path(urllib.parse.urlparse(url).path).name or "download.bin")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {common.env_value(common.ENV_BOT_TOKEN)}"})
    try:
        with urllib.request.build_opener(_SlackOnlyRedirects).open(req, timeout=60) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
    except urllib.error.HTTPError as exc:
        die(f"download failed: HTTP {exc.code}", 1)
    except Exception as exc:
        die(f"download failed: {type(exc).__name__}", 1)
    if looks_like_login_page(ctype, data):
        die("Slack returned an HTML page instead of the file: the bot token is missing files:read "
            "(add it and reinstall the app) or the file is not shared in a conversation the bot is in.", 1)
    out.write_bytes(data)
    print(json.dumps({"ok": True, "path": str(out), "bytes": len(data), "content_type": ctype}))
    return 0


SLACK_FILE_HOSTS = ("slack.com", "slack-edge.com", "slack-files.com")


class _SlackOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within Slack, so the token never leaves Slack."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname or ""
        if not host.endswith(SLACK_FILE_HOSTS):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-Slack host refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def looks_like_login_page(content_type: str, data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return "text/html" in (content_type or "").lower() and (
        head.startswith(b"<!doctype html") or head.startswith(b"<html"))


def health(home: Path) -> dict:
    """Process health and task state, reported separately."""
    pid = _pid_alive(home)
    proc = {"running": bool(pid), "pid": pid}
    hb_path = home / "run" / "heartbeat.json"
    if hb_path.exists():
        try:
            h = json.loads(hb_path.read_text())
            proc.update(heartbeat_age=int(time.time()) - int(h.get("heartbeat_at", 0)),
                        connected=h.get("connected"), started_at=h.get("started_at"),
                        last_connected_at=h.get("last_connected_at"),
                        webhook_host=h.get("webhook_host"), bot_user_id=h.get("bot_user_id"),
                        app_id=h.get("app_id"))
        except Exception:
            proc["heartbeat"] = "unreadable"
    proc["healthy"] = bool(pid) and proc.get("connected") is True and proc.get("heartbeat_age", 999) < 120
    tasks: dict = {}
    try:
        import store as storemod
        st = storemod.open_store(home)
        counts = st.counts()
        threads = st.list_threads(200)
        by_state: dict = {}
        for t in threads:
            by_state[t["state"]] = by_state.get(t["state"], 0) + 1
        tasks = {"operations": counts, "threads": by_state,
                 "needs_decision": [o["op_id"] for o in st.list_ops(
                     states=["needs-reconciliation", "unknown-result"], limit=20)],
                 "failed_recent": [o["op_id"] for o in st.list_ops(states=["failed"], limit=5)],
                 "webhook_failures_in_a_row": st.counter("webhook_failures")}
    except Exception as exc:
        tasks = {"error": type(exc).__name__}
    return {"process": proc, "tasks": tasks}


def cmd_health(args) -> int:
    home = common.resolve_home(args.home)
    h = health(home)
    if args.json:
        print(json.dumps(h, indent=2))
    else:
        p, t = h["process"], h["tasks"]
        print("== process")
        if p["running"]:
            print(f"  running pid {p['pid']}, heartbeat {p.get('heartbeat_age', '?')}s ago, "
                  f"connected={p.get('connected')}, healthy={p['healthy']}")
            print(f"  bot {p.get('bot_user_id')} app {p.get('app_id')} webhook host {p.get('webhook_host')}")
        else:
            print("  not running (scripts/start.sh)")
        print("== tasks")
        if "error" in t:
            print(f"  state database unavailable ({t['error']})")
        else:
            ops = ", ".join(f"{k}={v}" for k, v in sorted(t["operations"].items())) or "none"
            print(f"  operations: {ops}")
            print("  threads: " + (", ".join(f"{k}={v}" for k, v in sorted(t["threads"].items())) or "none"))
            if t["needs_decision"]:
                print(f"  need a decision (slackctl.sh ops show/resolve): {', '.join(t['needs_decision'])}")
            if t["webhook_failures_in_a_row"]:
                print(f"  webhook problems in a row: {t['webhook_failures_in_a_row']}")
    return 0 if h["process"]["running"] else 1


def _fmt_time(t) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(float(t))) if t else "-"


def cmd_ops(args) -> int:
    home = common.resolve_home(args.home)
    import store as storemod
    st = storemod.open_store(home)
    if args.action == "list":
        states = args.state or None
        rows = st.list_ops(states, args.limit)
        if args.json:
            print(json.dumps([{k: r[k] for k in ("op_id", "state", "reason", "kind", "channel", "ts",
                                                   "actor", "actor_type", "attempts", "updated_at")}
                              for r in rows], indent=2, ensure_ascii=False))
            return 0
        print(json.dumps(st.counts()))
        for r in rows:
            print(f"{_fmt_time(r['updated_at'])}  {r['state']:<20} {r['op_id']:<34} {r['actor_type'] or '':<5} "
                  f"{r['channel']}:{r['ts']}  {r['reason'][:70]}")
        return 0
    if not args.op:
        die(f"ops {args.action} needs an operation id")
    row = st.get(args.op)
    if row is None:
        die(f"unknown operation {args.op}", 1)
    if args.action == "show":
        row = {k: v for k, v in row.items() if k not in ("payload", "envelope")}
        row["history"] = st.history(args.op)
        print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0
    try:
        if args.action == "resolve":
            st.transition(args.op, args.to, args.reason or "resolved by operator")
        elif args.action == "retry":
            if row["state"] in ("unknown-result", "needs-reconciliation") and not args.force:
                die(f"{args.op} is {row['state']}: the agent may already have received it. "
                    "Check the thread first; pass --force to send it again anyway.", 1)
            st.transition(args.op, "queued", args.reason or "operator retry", not_before=0)
    except Exception as exc:
        die(str(exc), 1)
    print(json.dumps({"ok": True, "op": args.op, "state": st.get(args.op)["state"]}))
    return 0


def cmd_threads(args) -> int:
    home = common.resolve_home(args.home)
    import store as storemod
    st = storemod.open_store(home)
    rows = st.list_threads(args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for t in rows:
        print(f"{_fmt_time(t['updated_at'])}  {t['state']:<9} task={t['task_id']} bot_turns={t['bot_turns']} "
              f"follow={t['following']} cursor={t['cursor_ts'] or '-'} catchup_ok={t['catchup_ok']}  "
              f"{t['channel']}/{t['root_ts']}  {t['state_reason'][:50]}")
    return 0


def cmd_session(args) -> int:
    client = web_client()
    home = common.resolve_home(args.home)
    if args.title and not args.status:
        resp = api_json(client, "agents.sessions.rename",
                        {"channel_id": args.channel, "thread_ts": args.thread_ts,
                         "title": args.title[:200]})
    else:
        if args.status == "processing" and recently_stopped(home, args.channel, args.thread_ts):
            die("the user pressed Stop in this thread; not re-opening it", 1)
        body = {"channel_id": args.channel, "thread_ts": args.thread_ts,
                "status": args.status or "active"}
        resp = api_json(client, "agents.sessions.setStatus", body)
        if resp.get("ok") and args.title:
            resp = api_json(client, "agents.sessions.rename",
                            {"channel_id": args.channel, "thread_ts": args.thread_ts,
                             "title": args.title[:200]})
    if not resp.get("ok"):
        die(f"session call failed: {resp.get('error')}", 1)
    print(json.dumps({k: resp.get(k) for k in ("ok", "status", "agent_status") if k in resp}))
    return 0


def cmd_react(args) -> int:
    client = web_client()
    fn = client.reactions_remove if args.remove else client.reactions_add
    try:
        fn(channel=args.channel, timestamp=args.ts, name=args.name.strip(":"))
    except Exception as exc:
        err = slack_error(exc)
        if err not in ("already_reacted", "no_reaction"):
            die(f"reaction failed: {err}", 1)
    print(json.dumps({"ok": True}))
    return 0


def cmd_thread(args) -> int:
    client = web_client()
    try:
        if args.ts:
            resp = client.conversations_replies(channel=args.channel, ts=args.ts, limit=args.limit)
        else:
            resp = client.conversations_history(channel=args.channel, limit=args.limit)
    except Exception as exc:
        die(f"could not read conversation: {slack_error(exc)}", 1)
    messages = resp.get("messages") or []
    if not args.ts:
        messages = list(reversed(messages))
    cfg = common.load_config(common.resolve_home(args.home))
    hidden = 0
    for m in messages:
        if not args.include_refused and not context_visible(cfg, m, args.channel, args.ts or m.get("ts", "")):
            hidden += 1
            continue
        who = m.get("user") or m.get("bot_id") or "?"
        print(f"[{m.get('ts')}] {who}: {m.get('text', '')}")
    if hidden:
        print(f"({hidden} message(s) from senders the access policy refuses were hidden; "
              "--include-refused shows them)")
    return 0


def context_visible(cfg: dict, m: dict, channel: str, root_ts: str) -> bool:
    """Refused senders' messages stay out of the model's context by default."""
    import access
    ident = _ident(cfg)
    bot_id, app_id = m.get("bot_id") or "", m.get("app_id") or ""
    if (cfg.get("bot_user_id") and m.get("user") == cfg.get("bot_user_id")) or \
            (ident.app_id and app_id == ident.app_id):
        return True  # our own messages
    actor = access.Actor(user=m.get("user") or "", bot_id=bot_id, app_id=app_id,
                         is_bot=bool(bot_id or app_id or m.get("subtype") == "bot_message"),
                         team_id=ident.team_id, api_app_id=ident.app_id,
                         user_team="" if (bot_id or app_id) else (m.get("user_team") or m.get("team") or ""))
    entry = "dm" if channel.startswith("D") else "thread_follow"
    return access.check(cfg, ident, actor, channel=channel, root_ts=root_ts, entry=entry).allowed


def _ident(cfg: dict):
    import events
    return events.Identity(team_id=cfg.get("team_id") or "", app_id=cfg.get("app_id") or "",
                           bot_user_id=cfg.get("bot_user_id") or "", bot_id="")


def cmd_access(args) -> int:
    """Explain what the access policy decides for an actor (no Slack calls)."""
    import access
    cfg = common.load_config(common.resolve_home(args.home))
    ident = _ident(cfg)
    if args.action == "validate":
        problems = access.validate(cfg)
        for p in problems:
            print(f"- {p}")
        print("ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    actor = access.Actor(user=args.user or "", bot_id=args.bot_id or "", app_id=args.app_id or "",
                         is_bot=bool(args.bot_id or args.app_id), team_id=args.team or ident.team_id,
                         api_app_id=args.api_app_id or ident.app_id)
    v = access.check(cfg, ident, actor, channel=args.channel or "", root_ts=args.thread_ts or "",
                     entry=args.entry)
    print(json.dumps({"allowed": v.allowed, "reason": v.reason, "role": v.role,
                      "permissions": v.permissions,
                      "policy": {k: (sorted(x) if isinstance(x, set) else x) for k, x in v.policy.items()}},
                     indent=2, ensure_ascii=False))
    return 0 if v.allowed else 3


def cmd_routing(args) -> int:
    """Show the effective session route per channel (env var names only)."""
    cfg = common.load_config(common.resolve_home(args.home))
    env = {n: common.env_value(n) for n in common.route_env_names(cfg)}
    channels = [args.channel] if args.channel else \
        list(((cfg.get("session_routing") or {}).get("channels") or {}).keys())
    out = {"default": common.route_for(cfg, "", env),
           "channels": {ch: common.route_for(cfg, ch, env) for ch in channels},
           "problems": common.validate_routing(cfg)}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{6,}$")


def _backup_config(path: Path) -> Path | None:
    """Copy config.json to config.json.bak-<epoch> (mode 0600). Returns the backup path."""
    if not path.exists():
        return None
    backup = path.with_name(f"config.json.bak-{int(time.time())}")
    n = 0
    while backup.exists():
        n += 1
        backup = path.with_name(f"config.json.bak-{int(time.time())}-{n}")
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(path.read_text(encoding="utf-8"))
    return backup


def _route_env_problem(flag: str, name: str) -> str | None:
    """Why NAME is not usable as a dedicated route's env var name. Never echoes NAME
    when it looks like a value (URL, token, header), because it may be a secret."""
    if "://" in name or common.SECRET_PATTERN.search(name) or " " in name.strip():
        return (f"{flag} looks like a secret value (URL, token or Authorization header). "
                "Pass the environment variable NAME, e.g. GROK_WEBHOOK_URL_DEV; the value stays "
                "in the environment / box secrets only. Nothing was written.")
    if not common.ENV_NAME_RE.match(name):
        return f"{flag} must be an env var name (A-Z, 0-9, _; e.g. GROK_WEBHOOK_URL_DEV)"
    if name in common.REQUIRED_ENV:
        return f"{flag} reuses {name}; a dedicated agent needs its own variables"
    return None


def cmd_route(args) -> int:
    """Add or remove one channel's session route (dedicated agent webhook or main)."""
    home = common.resolve_home(args.home)
    path = common.config_path(home)
    channel = (args.channel or "").strip()
    if not CHANNEL_ID_RE.match(channel):
        die("--channel must be a Slack channel ID (C… or G…); DMs always use the main conversation")
    # Edit the file as written (not the defaults-merged view), like migrate-config.
    try:
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        die(f"{path} is not valid JSON ({exc}); fix it or restore a backup first")
    if not isinstance(cfg, dict):
        die(f"{path} must contain a JSON object")
    routing = dict(cfg.get("session_routing") or {})
    routing.setdefault("default", "main")
    channels = dict(routing.get("channels") or {})
    old = channels.get(channel)

    if args.action == "remove":
        if old is None:
            print(f"no session route for {channel}; nothing to do")
            return 0
        channels.pop(channel)
        routing["channels"] = channels
        cfg["session_routing"] = routing
        print(f"removing route for {channel}: {json.dumps(old, ensure_ascii=False)}")
        if args.dry_run:
            print("(dry run; nothing written)")
            return 0
        backup = _backup_config(path)
        if backup:
            print(f"backup: {backup}")
        common.save_config(home, cfg)
        print(f"updated {path}")
        print("\nNext steps:\n"
              f"  1. Takes effect live (no restart): new messages in {channel} go to the main "
              "conversation.\n"
              f"  2. Check: slackctl.sh routing --channel {channel}  (expect target main, source default)\n"
              "  3. Optional cleanup: pause or delete the dedicated agent's webhook routine, delete its "
              "two box secrets / env vars, keep or archive the channel memory file.\n"
              "  4. Variables of a removed route are dropped from the bridge at its next restart.")
        return 0

    # add / replace
    target = args.target
    entry: dict = {"target": target}
    label = (args.label or "").strip()
    if label:
        if "://" in label or common.SECRET_PATTERN.search(label):
            die("--label looks like a URL or token; use a plain name. Nothing was written.")
        entry["label"] = label
    if target == "dedicated":
        if not (args.url_env and args.auth_env):
            die("a dedicated route needs --url-env NAME and --auth-env NAME (env var names, not values)")
        for flag, name in (("--url-env", args.url_env.strip()), ("--auth-env", args.auth_env.strip())):
            problem = _route_env_problem(flag, name)
            if problem:
                die(problem)
        if args.url_env.strip() == args.auth_env.strip():
            die("--url-env and --auth-env must be two different variables")
        entry["webhook_url_env"] = args.url_env.strip()
        entry["webhook_auth_env"] = args.auth_env.strip()
    elif args.url_env or args.auth_env:
        die("--url-env/--auth-env only apply to --target dedicated")
    if args.busy_policy:
        entry["busy_policy"] = args.busy_policy

    if old is not None and old != entry and not args.replace:
        die(f"{channel} already has a route: {json.dumps(old, ensure_ascii=False)}\n"
            "  pass --replace to overwrite it (a backup is made), or remove it first")

    env_ok = True
    if target == "dedicated":
        for name in (entry["webhook_url_env"], entry["webhook_auth_env"]):
            val = common.env_value(name)
            if not val:
                state, env_ok = "missing", False
            elif name == entry["webhook_url_env"] and not val.lower().startswith("https://"):
                state, env_ok = "set, but not an https:// URL", False
            else:
                state = "set"
            print(f"env {name}: {state}")
        if not env_ok and not args.allow_missing_env:
            die("the route's env vars are not usable in this shell. Save the webhook URL and "
                "Authorization value as secrets / export them first (never paste them in chat), "
                "or pass --allow-missing-env (until they exist the bridge delivers this channel "
                "to the main conversation). Nothing was written.")

    routing["channels"] = dict(channels, **{channel: entry})
    cfg["session_routing"] = routing
    problems = common.validate_routing(cfg)
    if problems:
        die("refusing to write an invalid route: " + "; ".join(problems))
    print(f"route for {channel}: {json.dumps(entry, ensure_ascii=False)}"
          + (" (unchanged)" if old == entry else (" (replaces " + json.dumps(old, ensure_ascii=False) + ")"
                                                   if old is not None else "")))
    if args.dry_run:
        print("(dry run; nothing written)")
        return 0
    if old != entry:
        backup = _backup_config(path)
        if backup:
            print(f"backup: {backup}")
        try:
            common.save_config(home, cfg)
        except ValueError as exc:
            die(str(exc))
        print(f"updated {path}")
    steps = []
    if target == "dedicated":
        steps.append("Restart so the bridge reads the new env vars (it reads secrets only at start): "
                     "scripts/restart.sh  (or re-run with --restart)")
    else:
        steps.append("Takes effect live; no restart needed for a main route / busy_policy change")
    steps += [f"Check: slackctl.sh routing --channel {channel}  (expect target {target}"
              + (", webhook dedicated)" if target == "dedicated" else ")"),
              "Check: doctor.sh  (route line shows TLS ok; no request is sent)"]
    if target == "dedicated":
        steps += [f"Make sure the bot is in the channel (/invite) and create the channel memory file "
                  f"state/channels/{channel}.md (template: references/channel-memory-template.md)",
                  "Test: @mention the bot in the channel; the dedicated agent should answer, "
                  "and slackctl.sh ops list should show the operation completed",
                  f"Roll back any time: slackctl.sh route remove --channel {channel}"]
    print("\nNext steps:")
    for i, s_ in enumerate(steps, 1):
        print(f"  {i}. {s_}")
    return 0


def cmd_migrate_config(args) -> int:
    home = common.resolve_home(args.home)
    path = common.config_path(home)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    new, notes = common.migrate_config(data)
    for n in notes:
        print(f"- {n}")
    if not notes:
        print("config already up to date")
        return 0
    if args.dry_run:
        print("(dry run; nothing written)")
        return 0
    backup = path.with_name(f"config.json.bak-{int(time.time())}")
    if path.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"backup: {backup}")
    common.save_config(home, new)
    print(f"updated {path}")
    return 0


def cmd_whoami(args) -> int:
    client = web_client()
    try:
        auth = client.auth_test()
    except Exception as exc:
        die(f"auth.test failed: {slack_error(exc)}", 1)
    print(json.dumps({k: auth.get(k) for k in
                      ("team", "team_id", "url", "user", "user_id", "bot_id")}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def cmd_config(args) -> int:
    home = common.resolve_home(args.home)
    cfg = common.load_config(home)
    if args.action == "show":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0
    if args.action == "path":
        print(common.config_path(home))
        return 0
    if not args.key:
        die("config set/unset needs KEY")
    if args.action == "set":
        if args.value is None:
            die("config set needs KEY VALUE")
        try:
            cfg[args.key] = common.coerce_config_value(args.key, args.value)
        except ValueError as exc:
            die(str(exc))
    elif args.action == "unset":
        cfg.pop(args.key, None)
    try:
        path = common.save_config(home, cfg)
    except ValueError as exc:
        die(str(exc))
    print(f"updated {path}: {args.key} = {json.dumps(cfg.get(args.key), ensure_ascii=False)}")
    return 0


def cmd_set_owner(args) -> int:
    home = common.resolve_home(args.home)
    user_id = args.user_id.strip().strip("<@>")
    if not user_id.startswith(("U", "W")):
        die(f"{user_id!r} does not look like a Slack member ID (U… or W…). "
            "In Slack: profile -> ⋮ -> Copy member ID.")
    cfg = common.load_config(home)
    cfg["owner_user_id"] = user_id
    if common.env_value(common.ENV_BOT_TOKEN):
        try:
            info = web_client().users_info(user=user_id).get("user") or {}
            print(f"owner resolved: {info.get('real_name') or info.get('name')} ({user_id})")
        except Exception as exc:
            die(f"users.info could not resolve {user_id}: {slack_error(exc)}", 1)
    else:
        print("note: SLACK_BOT_TOKEN not set, owner ID saved without verification")
    path = common.save_config(home, cfg)
    print(f"updated {path}: owner_user_id = {user_id}")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _pid_alive(home: Path) -> int | None:
    pidfile = home / "run" / "bridge.pid"
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _post_json(url: str, data: dict, headers: dict, timeout: float = 15):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def check_webhook_tls(url: str, timeout: float = 8) -> tuple[bool, str]:
    """Connect + TLS handshake only; sends no HTTP request (does not wake the agent)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False, "not an https URL"
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=timeout) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname):
                return True, f"TLS ok to {parsed.hostname}"
    except Exception as exc:
        return False, f"{parsed.hostname}: {exc}"


def ack_scope_check(cfg: dict, auth) -> tuple[bool | None, str, str]:
    """doctor row: can the configured receipt ack work with the granted scopes?"""
    ack = common.ack_settings(cfg)
    scopes = common.granted_scopes(auth)
    name = "receipt ack"
    if ack["mode"] == "none":
        return True, name, "ack.mode=none (no receipt acknowledgement)"
    if scopes is None:
        return None, name, f"ack.mode={ack['mode']}; granted scopes unknown (no x-oauth-scopes header)"
    react, status = "reactions:write" in scopes, "assistant:write" in scopes
    if ack["mode"] == "reaction" and not react:
        return None, name, ("reactions:write not granted: :%s: cannot be added; falls back to the "
                            "assistant status %s. Reinstall the app in Slack after adding the scope."
                            % (ack["emoji"], "in DMs/agent threads" if status else "(also unavailable)"))
    if ack["mode"] == "status" and not status:
        return None, name, "assistant:write not granted: status ack unavailable, using the reaction"
    detail = f":{ack['emoji']}: reaction" if ack["mode"] == "reaction" else f"assistant status '{ack['status_text']}'"
    return True, name, (f"ack.mode={ack['mode']}: {detail}; reactions:write={'yes' if react else 'no'}, "
                        f"assistant:write={'yes' if status else 'no'}")


def cmd_doctor(args) -> int:
    home = common.resolve_home(args.home)
    results: list[tuple[str, str, str]] = []  # (status, check, detail)

    def add(ok: bool | None, check: str, detail: str):
        results.append(({True: "ok", False: "FAIL", None: "warn"}[ok], check, detail))

    # install
    venv_py = home / ".venv" / "bin" / "python"
    add(venv_py.exists(), "venv", str(venv_py) if venv_py.exists() else
        f"missing {venv_py}; run scripts/install.sh {home}")
    try:
        import slack_sdk  # noqa: F401
        add(True, "slack_sdk", f"version {slack_sdk.version.__version__}")
        have_sdk = True
    except ImportError:
        add(False, "slack_sdk", "not importable by this python; run via scripts/doctor.sh")
        have_sdk = False

    cfg_file = common.config_path(home)
    cfg = {}
    try:
        cfg = common.load_config(home)
        add(cfg_file.exists(), "config.json", str(cfg_file) if cfg_file.exists()
            else "missing; run scripts/install.sh")
    except Exception as exc:
        add(False, "config.json", f"unreadable: {exc}")
    add(bool(cfg.get("owner_user_id")) or None, "owner_user_id",
        cfg.get("owner_user_id") or "unset (scripts/set-owner.sh U…); payloads will say is_owner=false")
    try:
        import access
        problems = access.validate(cfg)
        add(None if problems else True, "access policy",
            "; ".join(problems) if problems else
            f"human_access={cfg.get('human_access')} bot_access={cfg.get('bot_access')} "
            f"bot_allowlist={len(cfg.get('bot_allowlist') or [])} "
            f"overrides={len(cfg.get('channel_overrides') or {})}")
    except Exception as exc:
        add(False, "access policy", str(exc))
    rproblems = common.validate_routing(cfg)
    routes = (cfg.get("session_routing") or {}).get("channels") or {}
    add(None if rproblems else True, "session routing",
        "; ".join(rproblems) if rproblems else
        f"default=main busy_policy={cfg.get('busy_policy', 'interrupt_merge')} channel routes={len(routes)}")
    for ch, entry in routes.items():
        if not isinstance(entry, dict) or entry.get("target") != "dedicated":
            continue
        env = {n: common.env_value(n) for n in common.route_env_names({"session_routing": {"channels": {ch: entry}}})}
        r = common.route_for(cfg, ch, env)
        if r.get("webhook") == "dedicated":
            ok, detail = check_webhook_tls(env[r["webhook_url_env"]])
            add(ok, f"route {ch}", f"dedicated via {r['webhook_url_env']}: {detail} (no request sent)")
        else:
            add(None, f"route {ch}", f"dedicated route unusable, falls back to main: {r.get('fallback')}")

    # env
    for name in common.REQUIRED_ENV:
        desc = common.describe_secret(name)
        add(desc.startswith("set ("), f"env {name}", desc)

    # Slack tokens
    if have_sdk and common.env_value(common.ENV_BOT_TOKEN):
        from slack_sdk import WebClient
        try:
            auth = WebClient(token=common.env_value(common.ENV_BOT_TOKEN)).auth_test()
            add(True, "bot token (auth.test)",
                f"@{auth.get('user')} {auth.get('user_id')} in {auth.get('team')} ({auth.get('team_id')})")
            add(*ack_scope_check(cfg, auth))
            if cfg_file.exists():
                changed = False
                for key, val in (("bot_user_id", auth.get("user_id")), ("team_id", auth.get("team_id")),
                                 ("workspace", auth.get("team")), ("workspace_url", auth.get("url"))):
                    if val and cfg.get(key) != val:
                        cfg[key] = val
                        changed = True
                if changed:
                    common.save_config(home, cfg)
        except Exception as exc:
            add(False, "bot token (auth.test)", slack_error(exc))
    if common.env_value(common.ENV_APP_TOKEN).startswith("xapp-"):
        # apps.connections.open only issues a websocket URL; it does not connect.
        status, data = _post_json("https://slack.com/api/apps.connections.open", {},
                                  {"Authorization": f"Bearer {common.env_value(common.ENV_APP_TOKEN)}"})
        if data.get("ok"):
            add(True, "app token (apps.connections.open)", "valid, Socket Mode enabled")
        else:
            hint = {"invalid_auth": "token revoked or wrong", "not_allowed_token_type":
                    "needs an app-level xapp- token with connections:write"}.get(data.get("error"), "")
            add(False, "app token (apps.connections.open)", f"{data.get('error') or status} {hint}".strip())

    # webhook
    url = common.env_value(common.ENV_WEBHOOK_URL)
    if url:
        ok, detail = check_webhook_tls(url)
        add(ok, "webhook reachability", detail + " (no request sent)")
        if args.ping_webhook:
            auth = common.normalize_auth_header(common.env_value(common.ENV_WEBHOOK_AUTH))
            ping = {"source": "slack-bridge", "type": "bridge_ping", "version": 1,
                    "bot_name": cfg.get("bot_name"), "sent_at": int(time.time()),
                    "note": "Connectivity test from slackctl doctor --ping-webhook. No reply needed."}
            try:
                status, _ = _post_json(url, ping, {"Authorization": auth} if auth else {})
                add(200 <= status < 300, "webhook ping", f"HTTP {status}" + (
                    "" if 200 <= status < 300 else " (stale URL/Authorization?)"))
            except Exception as exc:
                add(False, "webhook ping", str(exc))

    # process
    pid = _pid_alive(home)
    hb = home / "run" / "heartbeat.json"
    detail = f"running pid {pid}" if pid else "not running (scripts/start.sh)"
    if pid and hb.exists():
        try:
            h = json.loads(hb.read_text())
            age = int(time.time()) - int(h.get("heartbeat_at", 0))
            detail += f", heartbeat {age}s ago, connected={h.get('connected')}, " \
                      f"forwarded={h.get('forwarded')}, failed={h.get('failed')}"
        except Exception:
            pass
    add(True if pid else None, "bridge process", detail)
    lock_path = home / "run" / "bridge.lock"
    if pid:
        add(None if not lock_path.exists() else True, "single-instance lock",
            str(lock_path) if lock_path.exists() else
            "no run/bridge.lock: the running bridge predates the lock (restart.sh to pick up new code)")
    t = health(home)["tasks"]
    if "error" in t:
        add(None, "task state", f"state database unavailable ({t['error']})")
    else:
        attention = len(t["needs_decision"])
        ops = ", ".join(f"{k}={v}" for k, v in sorted(t["operations"].items())) or "none"
        add(None if attention or t["webhook_failures_in_a_row"] else True, "task state",
            f"{ops}" + (f"; {attention} need a decision (slackctl.sh ops list --state needs-reconciliation)"
                        if attention else "")
            + (f"; {t['webhook_failures_in_a_row']} webhook problems in a row"
               if t["webhook_failures_in_a_row"] else ""))

    if args.json:
        print(json.dumps([{"status": s, "check": c, "detail": d} for s, c, d in results], indent=2))
    else:
        width = max(len(c) for _, c, _ in results)
        for s, c, d in results:
            print(f"[{s:>4}] {c:<{width}}  {d}")
    sys.stdout.flush()
    failed = [c for s, c, _ in results if s == "FAIL"]
    if failed:
        print(f"\n{len(failed)} problem(s): " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slackctl", description=__doc__.splitlines()[0])
    p.add_argument("--home", help="install directory (default: $SLACK_BRIDGE_HOME or scripts/..)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reply", help="post a message as the bot")
    r.add_argument("--channel", required=True)
    r.add_argument("--thread-ts", help="reply inside this thread")
    r.add_argument("--ack-ts", help="message the bridge acknowledged; its receipt ack (the reaction, or "
                   "the assistant status recorded for --op) is removed after the final reply")
    r.add_argument("--ack-reaction", help="reaction name to remove (default: config ack.emoji)")
    r.add_argument("--done-reaction", help="optional reaction to add to --ack-ts after replying")
    r.add_argument("--format", choices=("markdown", "mrkdwn", "plain"), default="markdown")
    r.add_argument("--op", help="operation_id from the payload; marks it completed after posting")
    r.add_argument("--no-reply", action="store_true",
                   help="send nothing; record that the agent saw the message and chose not to reply")
    r.add_argument("--reason", help="note stored with --no-reply")
    r.add_argument("--allow-mention", action="append", metavar="U…",
                   help="user ID this reply may ping (repeatable); others are rendered inert")
    r.add_argument("--force", action="store_true",
                   help="post even if the task/thread was stopped (manual messages only)")
    r.add_argument("--session-status", choices=SESSION_STATUSES + ("none",),
                   help="after posting, set the thread's agent session status: active (done, "
                        "the default in payload commands), processing (an interim 'working on "
                        "it' message), suspended (waiting for the user), closed, or none")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--text")
    g.add_argument("--text-file")
    r.set_defaults(func=cmd_reply)

    rx = sub.add_parser("react", help="add (or --remove) a reaction")
    rx.add_argument("--channel", required=True)
    rx.add_argument("--ts", required=True)
    rx.add_argument("--name", required=True)
    rx.add_argument("--remove", action="store_true")
    rx.set_defaults(func=cmd_react)

    t = sub.add_parser("thread", help="print a thread (--ts) or recent channel/DM history")
    t.add_argument("--channel", required=True)
    t.add_argument("--ts")
    t.add_argument("--limit", type=int, default=30)
    t.add_argument("--include-refused", action="store_true",
                   help="also show messages from senders the access policy refuses")
    t.set_defaults(func=cmd_thread)

    ac = sub.add_parser("access", help="explain the access decision for an actor, or validate config")
    ac.add_argument("action", choices=("check", "validate"))
    ac.add_argument("--user")
    ac.add_argument("--bot-id")
    ac.add_argument("--app-id")
    ac.add_argument("--channel")
    ac.add_argument("--thread-ts")
    ac.add_argument("--entry", default="mention", choices=("dm", "mention", "thread_follow", "channel",
                                                          "slash_command", "button"))
    ac.add_argument("--team", help="envelope team id (default: our team)")
    ac.add_argument("--api-app-id", help="envelope app id (default: our app)")
    ac.set_defaults(func=cmd_access)

    rt = sub.add_parser("routing", help="show which agent conversation handles each channel")
    rt.add_argument("--channel")
    rt.set_defaults(func=cmd_routing)

    ro = sub.add_parser("route", help="add/replace or remove one channel's session route (backs up config)")
    ro.add_argument("action", choices=("add", "remove"))
    ro.add_argument("--channel", required=True, help="channel ID (C… or G…)")
    ro.add_argument("--target", choices=common.ROUTE_TARGETS, default="dedicated")
    ro.add_argument("--label", help="human-readable name of the handling agent")
    ro.add_argument("--url-env", help="NAME of the env var holding the dedicated webhook URL")
    ro.add_argument("--auth-env", help="NAME of the env var holding its Authorization value")
    ro.add_argument("--busy-policy", choices=common.BUSY_POLICIES, help="per-channel busy_policy")
    ro.add_argument("--replace", action="store_true", help="overwrite an existing, different entry")
    ro.add_argument("--allow-missing-env", action="store_true",
                    help="write the route even if its env vars are not set in this shell")
    ro.add_argument("--dry-run", action="store_true")
    ro.set_defaults(func=cmd_route)

    mc = sub.add_parser("migrate-config", help="add missing access/routing keys (legacy access -> explicit policy)")
    mc.add_argument("--dry-run", action="store_true")
    mc.set_defaults(func=cmd_migrate_config)

    se = sub.add_parser("session", help="set an agent session's status and/or title")
    se.add_argument("--channel", required=True)
    se.add_argument("--thread-ts", required=True)
    se.add_argument("--status", choices=SESSION_STATUSES)
    se.add_argument("--title")
    se.set_defaults(func=cmd_session)

    sub.add_parser("whoami", help="auth.test for the bot token").set_defaults(func=cmd_whoami)

    op = sub.add_parser("ops", help="inspect and resolve delivery operations (state database)")
    op.add_argument("action", choices=("list", "show", "resolve", "retry"))
    op.add_argument("op", nargs="?")
    op.add_argument("--state", action="append", help="filter list by state (repeatable)")
    op.add_argument("--to", choices=("completed", "no_reply", "stopped", "ignored"), default="ignored",
                    help="target state for resolve")
    op.add_argument("--reason")
    op.add_argument("--force", action="store_true", help="retry even an unknown-result operation")
    op.add_argument("--limit", type=int, default=30)
    op.add_argument("--json", action="store_true")
    op.set_defaults(func=cmd_ops)

    th = sub.add_parser("threads", help="list tracked threads (task state, bot turns, cursor)")
    th.add_argument("--limit", type=int, default=30)
    th.add_argument("--json", action="store_true")
    th.set_defaults(func=cmd_threads)

    up = sub.add_parser("upload", help="upload a file to a channel/thread (files:write)")
    up.add_argument("--channel", required=True)
    up.add_argument("--thread-ts")
    up.add_argument("--file", required=True)
    up.add_argument("--filename")
    up.add_argument("--title")
    up.add_argument("--comment", help="message posted with the file")
    up.add_argument("--op", help="operation_id (refuses if the task was stopped)")
    up.add_argument("--force", action="store_true")
    up.set_defaults(func=cmd_upload)

    dl = sub.add_parser("download", help="download a Slack file by id or url_private (files:read)")
    dl.add_argument("--file-id")
    dl.add_argument("--url")
    dl.add_argument("--out", help="output path (default: the file's name)")
    dl.set_defaults(func=cmd_download)

    he = sub.add_parser("health", help="process health and task state (separately)")
    he.add_argument("--json", action="store_true")
    he.set_defaults(func=cmd_health)

    c = sub.add_parser("config", help="show/set non-secret config.json values")
    c.add_argument("action", choices=("show", "set", "unset", "path"))
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.set_defaults(func=cmd_config)

    o = sub.add_parser("set-owner", help="record the owner's Slack member ID")
    o.add_argument("user_id")
    o.set_defaults(func=cmd_set_owner)

    d = sub.add_parser("doctor", help="validate install, env, tokens, webhook, process")
    d.add_argument("--ping-webhook", action="store_true",
                   help="also POST a bridge_ping test payload (this wakes the agent once)")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("render-manifest", help="print a Slack app manifest for a custom bot name")
    m.add_argument("--name", default="Grok Bot")
    m.add_argument("--display-name")
    m.add_argument("--description", default="Chat with your Grok Bot assistant from Slack.")
    m.add_argument("--color", default="#111827")
    m.add_argument("--prompt", action="append", metavar="'Title|Message'",
                   help="suggested prompt shown in the agent view (repeat, max 4)")
    m.add_argument("--slash-command", default=DEFAULT_SLASH_COMMAND,
                   help="slash command to register (default /grok; 'none' to omit)")
    m.add_argument("--no-agent-view", action="store_true",
                   help="plain bot app without Slack's agent features")
    m.add_argument("--format", choices=("yaml", "json"), default="yaml")
    m.add_argument("--out")
    m.set_defaults(func=cmd_render_manifest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
