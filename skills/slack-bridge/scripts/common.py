"""Shared, dependency-free helpers for the Slack bridge.

Everything here is pure Python (standard library only) so it can be unit
tested without slack_sdk and imported by both bridge.py and slackctl.py.

Secrets are only ever read from environment variables and are never written
to disk or logs by this module.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Environment / secrets
# ---------------------------------------------------------------------------

ENV_BOT_TOKEN = "SLACK_BOT_TOKEN"
ENV_APP_TOKEN = "SLACK_APP_TOKEN"
ENV_WEBHOOK_URL = "GROK_WEBHOOK_URL"
ENV_WEBHOOK_AUTH = "GROK_WEBHOOK_AUTH"
ENV_HOME = "SLACK_BRIDGE_HOME"

REQUIRED_ENV = (ENV_BOT_TOKEN, ENV_APP_TOKEN, ENV_WEBHOOK_URL, ENV_WEBHOOK_AUTH)

TOKEN_PREFIXES = {
    ENV_BOT_TOKEN: "xoxb-",
    ENV_APP_TOKEN: "xapp-",
}


def env_value(name: str, environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    return (environ.get(name) or "").strip()


def describe_secret(name: str, environ: dict[str, str] | None = None) -> str:
    """Return a non-revealing description of an env secret for diagnostics."""
    value = env_value(name, environ)
    if not value:
        return "missing"
    prefix = TOKEN_PREFIXES.get(name)
    if prefix and not value.startswith(prefix):
        return f"set, but does not start with {prefix!r} (wrong token type?)"
    if name == ENV_WEBHOOK_URL and not value.lower().startswith("https://"):
        return "set, but is not an https:// URL"
    return f"set ({len(value)} chars)"


def check_env(names: Iterable[str] = REQUIRED_ENV,
              environ: dict[str, str] | None = None) -> list[str]:
    """Return human-readable problems with the given env vars (empty = ok)."""
    problems = []
    for name in names:
        desc = describe_secret(name, environ)
        if desc != "missing" and not desc.startswith("set ("):
            problems.append(f"{name}: {desc}")
        elif desc == "missing":
            problems.append(f"{name}: missing")
    return problems


def normalize_auth_header(value: str) -> str:
    """Accept 'Bearer x', 'Authorization: Bearer x' or a raw value."""
    value = (value or "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    return value


def webhook_host(url: str) -> str:
    """Host part of a URL, safe to print (no path, query, or credentials)."""
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]*@)?([^/:?#]+)", url or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Home directory / config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "bot_name": "Grok Bot",
    "agent_label": "",
    "workspace": "",
    "workspace_url": "",
    "team_id": "",
    "bot_user_id": "",
    "owner_user_id": "",
    "access": "everyone",
    "deny_message": "Sorry, I only take requests from my owner here.",
    "react_on_receipt": True,
    "ack_reaction": "eyes",
    "error_reaction": "warning",
    "dm_reply_in_thread": False,
    "forward_raw_event": True,
    "log_message_text": False,
    "webhook_timeout_seconds": 20,
    "webhook_retries": 3,
    # Slack agent features (manifest `features.agent_view` + `assistant:write`).
    # When on, each conversation becomes an agent session thread with a
    # "Working..." status instead of the receipt reaction. Falls back to the
    # plain behaviour automatically if Slack rejects the session call.
    "agent_sessions": True,
    "session_title_chars": 60,
    "stop_message": "Stopped.",
}

ACCESS_MODES = ("everyone", "owner_only")


def resolve_home(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_home = os.environ.get(ENV_HOME)
    if env_home:
        return Path(env_home).expanduser()
    # scripts/ lives directly inside the home (possibly via a symlink).
    return Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def config_path(home: Path) -> Path:
    return home / "config.json"


def load_config(home: Path) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path(home)
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object")
        cfg.update(data)
    return cfg


SECRET_PATTERN = re.compile(r"xox[abpre]-|xapp-|bearer\s", re.IGNORECASE)


def save_config(home: Path, cfg: dict[str, Any]) -> Path:
    """Atomically write config.json. Refuses anything that looks like a secret."""
    for key, value in cfg.items():
        if isinstance(value, str) and SECRET_PATTERN.search(value):
            raise ValueError(
                f"refusing to store a token-like value in config key {key!r}; "
                "secrets belong in environment variables only")
    home.mkdir(parents=True, exist_ok=True)
    path = config_path(home)
    fd, tmp = tempfile.mkstemp(dir=str(home), prefix=".config.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def coerce_config_value(key: str, raw: str) -> Any:
    default = DEFAULT_CONFIG.get(key)
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} expects true/false, got {raw!r}")
    if isinstance(default, int):
        return int(raw)
    if key == "access" and raw not in ACCESS_MODES:
        raise ValueError(f"access must be one of {ACCESS_MODES}")
    if key in ("ack_reaction", "error_reaction"):
        return raw.strip().strip(":")
    return raw


# ---------------------------------------------------------------------------
# Event filtering and payload building
# ---------------------------------------------------------------------------

# Agent (agent_view) events the bridge handles itself instead of forwarding.
AGENT_EVENTS = {"app_home_opened", "app_context_changed", "agent_session_stopped", "agent_session_title_changed"}

# Message subtypes that still represent a human writing to the bot.
ALLOWED_SUBTYPES = {None, "", "file_share", "thread_broadcast"}


def should_handle(event: dict[str, Any], bot_user_id: str = "",
                  bot_id: str = "") -> tuple[bool, str]:
    """Decide whether an Events API event should be forwarded."""
    etype = event.get("type")
    if etype not in ("app_mention", "message"):
        return False, f"unsupported event type {etype}"
    if etype == "message" and event.get("channel_type") not in ("im", None):
        # Plain channel messages are only forwarded via app_mention.
        return False, f"message in {event.get('channel_type')} (needs a mention)"
    subtype = event.get("subtype")
    if subtype not in ALLOWED_SUBTYPES:
        return False, f"subtype {subtype}"
    if event.get("bot_id") or event.get("bot_profile"):
        return False, "message from a bot"
    user = event.get("user")
    if not user:
        return False, "no user"
    if bot_user_id and user == bot_user_id:
        return False, "own message"
    if bot_id and event.get("bot_id") == bot_id:
        return False, "own bot_id"
    if event.get("hidden"):
        return False, "hidden"
    return True, "ok"


class Deduper:
    """Small LRU of seen keys (event_id and channel:ts)."""

    def __init__(self, size: int = 4000):
        self.size = size
        self._seen: OrderedDict[str, None] = OrderedDict()

    def seen(self, *keys: str) -> bool:
        keys = tuple(k for k in keys if k)
        hit = any(k in self._seen for k in keys)
        for k in keys:
            self._seen[k] = None
            self._seen.move_to_end(k)
        while len(self._seen) > self.size:
            self._seen.popitem(last=False)
        return hit


def strip_mention(text: str, bot_user_id: str) -> str:
    if not text:
        return ""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(\|[^>]*)?>", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def reply_target(event: dict[str, Any], dm_reply_in_thread: bool = False) -> dict[str, Any]:
    """Where a reply to this event should go."""
    channel = event.get("channel")
    is_dm = event.get("channel_type") == "im" or str(channel or "").startswith("D")
    thread_ts = event.get("thread_ts")
    if not thread_ts and (not is_dm or dm_reply_in_thread):
        thread_ts = event.get("ts")
    return {"channel": channel, "thread_ts": thread_ts}


def session_thread_ts(event: dict[str, Any]) -> str:
    """Root ts of the agent session thread for a message: its thread or itself."""
    return event.get("thread_ts") or event.get("ts") or ""


def session_title(text: str, limit: int = 60) -> str:
    """Short one-line session title from the first message text."""
    text = re.sub(r"<@[A-Z0-9]+(\|[^>]*)?>", "", text or "")
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"\s+", " ", text).strip()
    limit = max(10, min(int(limit or 60), 200))
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def context_channels(event: dict[str, Any]) -> list[str]:
    """Channel ids from an app_context_changed event, most relevant first."""
    entities = ((event.get("context") or {}).get("entities")) or []
    return [e.get("value") for e in entities
            if isinstance(e, dict) and e.get("type") == "slack#/types/channel_id" and e.get("value")]


def shell_quote(value: str) -> str:
    return shlex.quote(str(value))


def build_payload(envelope: dict[str, Any], cfg: dict[str, Any], home: Path,
                  user_info: dict[str, Any] | None = None,
                  session: dict[str, Any] | None = None,
                  viewing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Webhook payload for one message.

    session: {"channel", "thread_ts", "status"} when the bridge opened/updated an
    agent session for this message (replies then go in that thread and the
    reply command ends the session's "processing" state). None otherwise.
    viewing: {"channel_ids": [...], "updated_at": int} last app_context_changed
    for this user (what they were looking at in Slack), if known.
    """
    event = envelope.get("event") or {}
    bot_user_id = cfg.get("bot_user_id", "")
    if session:
        target = {"channel": session.get("channel") or event.get("channel"),
                  "thread_ts": session.get("thread_ts")}
    else:
        target = reply_target(event, bool(cfg.get("dm_reply_in_thread")))
    user = event.get("user", "")
    owner = cfg.get("owner_user_id") or ""
    is_dm = event.get("channel_type") == "im"
    reply_cmd = [str(home / "scripts" / "reply.sh"), "--channel", target["channel"] or ""]
    if target["thread_ts"]:
        reply_cmd += ["--thread-ts", target["thread_ts"]]
    if cfg.get("react_on_receipt") and cfg.get("ack_reaction") and not session:
        reply_cmd += ["--ack-ts", event.get("ts", "")]
    if session:
        reply_cmd += ["--session-status", "active"]
    payload: dict[str, Any] = {
        "source": "slack-bridge",
        "version": 1,
        "bot_name": cfg.get("bot_name"),
        "bot_user_id": bot_user_id,
        "team_id": envelope.get("team_id") or event.get("team"),
        "workspace": cfg.get("workspace"),
        "event_id": envelope.get("event_id"),
        "event_time": envelope.get("event_time"),
        "event_type": event.get("type"),
        "conversation": "dm" if is_dm else (event.get("channel_type") or "channel"),
        "channel": event.get("channel"),
        "user": user,
        "user_name": (user_info or {}).get("name", ""),
        "user_real_name": (user_info or {}).get("real_name", ""),
        "is_owner": bool(owner) and user == owner,
        "owner_configured": bool(owner),
        "text": strip_mention(event.get("text", ""), bot_user_id),
        "ts": event.get("ts"),
        "thread_ts": event.get("thread_ts"),
        "files": [
            {k: f.get(k) for k in ("id", "name", "mimetype", "size", "permalink")}
            for f in event.get("files") or []
        ],
        "reply": {
            **target,
            "command": " ".join(shell_quote(p) for p in reply_cmd) + " <<'EOF'\n<your reply>\nEOF",
            "readme": str(home / "README.md"),
        },
        "agent_session": ({"channel": target["channel"], "thread_ts": target["thread_ts"],
                           "status": session.get("status", "processing")} if session else None),
        "viewing_context": viewing or None,
    }
    if cfg.get("forward_raw_event", True):
        payload["raw_event"] = event
    return payload


# ---------------------------------------------------------------------------
# Reply formatting
# ---------------------------------------------------------------------------

MAX_MARKDOWN_BLOCK = 11000  # Slack markdown blocks allow 12k chars per payload.


def chunk_text(text: str, limit: int = MAX_MARKDOWN_BLOCK) -> list[str]:
    """Split text into chunks <= limit, preferring paragraph/line boundaries.

    Avoids splitting inside fenced code blocks when possible by closing and
    reopening the fence across chunks.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        piece, rest = rest[:cut], rest[cut:].lstrip("\n")
        if piece.count("```") % 2 == 1:
            piece += "\n```"
            rest = "```\n" + rest
        chunks.append(piece)
    if rest:
        chunks.append(rest)
    return chunks


def markdown_to_mrkdwn(text: str) -> str:
    """Best-effort conversion of common Markdown to Slack mrkdwn (fallback path)."""
    out_lines = []
    in_code = False
    for line in (text or "").split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            out_lines.append("```")
            continue
        if in_code:
            out_lines.append(line)
            continue
        line = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"__(.+?)__", r"*\1*", line)
        line = re.sub(r"~~(.+?)~~", r"~\1~", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", line)
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        out_lines.append(line)
    return "\n".join(out_lines)
