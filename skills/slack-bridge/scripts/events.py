"""Normalize Slack events into one message shape (pure, no network)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import store

# Message subtypes that still represent someone writing a message.
ALLOWED_SUBTYPES = {None, "", "file_share", "thread_broadcast", "bot_message", "me_message"}
MESSAGE_EVENTS = ("message", "app_mention")


@dataclass
class Identity:
    """Who this bridge is (from auth.test / bots.info)."""
    team_id: str = ""
    app_id: str = ""
    bot_user_id: str = ""
    bot_id: str = ""


@dataclass
class Msg:
    op_id: str
    event_type: str
    team_id: str
    api_app_id: str
    channel: str
    channel_type: str
    ts: str
    thread_ts: str
    root_ts: str
    text: str
    user: str
    bot_id: str
    app_id: str
    user_team: str
    actor_type: str           # human | bot
    is_dm: bool
    mentions_bot: bool
    files: list = field(default_factory=list)
    catchup: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def actor(self) -> str:
        return self.user or self.bot_id or self.app_id

    @property
    def msg_key(self) -> str | None:
        return f"{self.channel}:{self.ts}" if self.ts else None

    @property
    def fingerprint(self) -> str:
        return store.message_fingerprint(self.channel, self.ts, self.actor, self.text)

    def actor_obj(self):
        import access
        return access.Actor(user=self.user, bot_id=self.bot_id, app_id=self.app_id,
                            is_bot=self.actor_type == "bot", team_id=self.team_id,
                            user_team=self.user_team if self.actor_type == "human" else "",
                            api_app_id=self.api_app_id)

    def thread_key(self, ident: Identity) -> str:
        return store.thread_key(self.team_id or ident.team_id, self.channel, self.root_ts,
                                ident.app_id or self.api_app_id)


def mention_pattern(bot_user_id: str) -> re.Pattern:
    return re.compile(rf"<@{re.escape(bot_user_id)}(\|[^>]*)?>") if bot_user_id else re.compile(r"(?!)")


def normalize(envelope: dict[str, Any], ident: Identity,
              catchup: bool = False) -> tuple[Msg | None, str]:
    """Return (Msg, "ok") or (None, reason) for noise that needs no receipt.

    Noise = edits/deletes/joins and other subtypes, hidden messages, our own
    messages, malformed events. Everything else gets a receipt and a decision.
    """
    event = envelope.get("event") or {}
    etype = event.get("type")
    if etype not in MESSAGE_EVENTS:
        return None, f"not a message event ({etype})"
    subtype = event.get("subtype")
    if subtype not in ALLOWED_SUBTYPES:
        return None, f"subtype {subtype}"
    if event.get("hidden"):
        return None, "hidden"
    channel, ts = event.get("channel") or "", event.get("ts") or ""
    if not channel or not ts:
        return None, "no channel/ts"
    bot_profile = event.get("bot_profile") or {}
    user = event.get("user") or ""
    bot_id = event.get("bot_id") or bot_profile.get("id") or ""
    app_id = event.get("app_id") or bot_profile.get("app_id") or ""
    if (ident.bot_user_id and user == ident.bot_user_id) or (ident.bot_id and bot_id == ident.bot_id) \
            or (ident.app_id and app_id == ident.app_id):
        return None, "own message"
    is_bot = bool(bot_id or bot_profile or subtype == "bot_message")
    text = event.get("text") or ""
    channel_type = event.get("channel_type") or ("im" if channel.startswith("D") else "")
    thread_ts = event.get("thread_ts") or ""
    msg = Msg(
        op_id=str(envelope.get("event_id") or ""),
        event_type=etype,
        team_id=str(envelope.get("team_id") or event.get("team") or ""),
        api_app_id=str(envelope.get("api_app_id") or ""),
        channel=channel,
        channel_type=channel_type,
        ts=ts,
        thread_ts=thread_ts,
        root_ts=thread_ts or ts,
        text=text,
        user=user,
        bot_id=bot_id,
        app_id=app_id,
        user_team=str(event.get("user_team") or event.get("team") or ""),
        actor_type="bot" if is_bot else "human",
        is_dm=channel_type == "im",
        mentions_bot=bool(mention_pattern(ident.bot_user_id).search(text)) or etype == "app_mention",
        files=[{k: f.get(k) for k in ("id", "name", "mimetype", "size", "permalink", "url_private_download")}
               for f in event.get("files") or []],
        catchup=catchup,
        raw=event,
    )
    if not msg.op_id:
        msg.op_id = f"msg:{channel}:{ts}"
    if not msg.actor:
        return None, "unknown author"
    return msg, "ok"


def catchup_envelope(message: dict[str, Any], channel: str, channel_type: str,
                     ident: Identity) -> dict[str, Any]:
    """Wrap a conversations.replies message as an Events API envelope."""
    event = dict(message)
    event.setdefault("type", "message")
    event["channel"] = channel
    event["channel_type"] = channel_type
    return {"event_id": f"catchup:{channel}:{message.get('ts')}", "team_id": ident.team_id,
            "api_app_id": ident.app_id, "event": event, "catchup": True}


def slash_message(payload: dict[str, Any], dm_channel: str) -> Msg:
    """A /command invocation as a Msg (replies go to the user's DM with the bot)."""
    text = (payload.get("text") or "").strip()
    user = payload.get("user_id") or ""
    trigger = payload.get("trigger_id") or ""
    return Msg(
        op_id=f"cmd:{trigger or store.fingerprint(user, text, payload.get('channel_id'))}",
        event_type="slash_command", team_id=str(payload.get("team_id") or ""),
        api_app_id=str(payload.get("api_app_id") or ""), channel=dm_channel, channel_type="im",
        ts="", thread_ts="", root_ts="", text=text, user=user, bot_id="", app_id="",
        user_team="", actor_type="human", is_dm=True, mentions_bot=True,
        raw={"type": "slash_command", "channel": dm_channel, "channel_type": "im", "user": user,
             "text": text, "command": payload.get("command"),
             "source_channel": payload.get("channel_id")})
