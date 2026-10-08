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

BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "chat:write",
    "groups:history",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "reactions:write",
    "users:read",
]
BOT_EVENTS = ["app_mention", "message.im"]


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
                   display_name: str | None = None) -> dict:
    return {
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
            lines.append(f"{pad}- {_yaml_scalar(item)}")
    return "\n".join(lines)


def cmd_render_manifest(args) -> int:
    manifest = build_manifest(args.name, args.description, args.color, args.display_name)
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


def cmd_reply(args) -> int:
    text = read_text(args).strip()
    if not text:
        die("refusing to send an empty message")
    client = web_client()
    try:
        sent = post_reply(client, args.channel, text, args.thread_ts, args.format)
    except Exception as exc:
        die(f"chat.postMessage failed: {slack_error(exc)}", 1)
    if args.ack_ts:
        cfg = common.load_config(common.resolve_home(args.home))
        name = args.ack_reaction or cfg.get("ack_reaction") or "eyes"
        try:
            client.reactions_remove(channel=args.channel, timestamp=args.ack_ts, name=name)
        except Exception as exc:
            if slack_error(exc) not in ("no_reaction",):
                print(f"slackctl: note: could not remove :{name}: ({slack_error(exc)})",
                      file=sys.stderr)
        if args.done_reaction:
            try:
                client.reactions_add(channel=args.channel, timestamp=args.ack_ts,
                                     name=args.done_reaction.strip(":"))
            except Exception:
                pass
    print(json.dumps({"ok": True, "channel": args.channel, "thread_ts": args.thread_ts,
                      "ts": sent}))
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
    for m in messages:
        who = m.get("user") or m.get("bot_id") or "?"
        print(f"[{m.get('ts')}] {who}: {m.get('text', '')}")
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
    r.add_argument("--ack-ts", help="remove the receipt reaction from this message after replying")
    r.add_argument("--ack-reaction", help="reaction name to remove (default: config ack_reaction)")
    r.add_argument("--done-reaction", help="optional reaction to add to --ack-ts after replying")
    r.add_argument("--format", choices=("markdown", "mrkdwn", "plain"), default="markdown")
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
    t.set_defaults(func=cmd_thread)

    sub.add_parser("whoami", help="auth.test for the bot token").set_defaults(func=cmd_whoami)

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
    m.add_argument("--format", choices=("yaml", "json"), default="yaml")
    m.add_argument("--out")
    m.set_defaults(func=cmd_render_manifest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
