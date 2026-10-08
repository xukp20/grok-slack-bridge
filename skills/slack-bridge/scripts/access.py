"""One access check for every entry point (pure, no network).

Entry points: dm, mention, thread_follow, channel, slash_command, button.

Order of evaluation (first match wins):
  1. the event must come from this workspace (team_id) and this app
     (api_app_id); otherwise refused
  2. unknown identity (no user and no bot/app id, or a human from another
     workspace) -> refused
  3. channel_allowlist (if non-empty) -> refused outside it (DMs exempt)
  4. denylists (user_denylist, bot_denylist, plus per-channel additions) match
     any of the actor's real IDs (user, bot, app) -> refused. Denylists win.
  5. humans: human_access everyone | allowlist | owner_only
     bots:   bot_access   none | allowlist | all
     Per-channel overrides can only make access stricter.

Bots are matched by real IDs only (user U…, bot B…, app A…), never by name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

HUMAN_ACCESS = ("everyone", "allowlist", "owner_only")     # loosest -> strictest
BOT_ACCESS = ("all", "allowlist", "none")                  # loosest -> strictest
TRIGGERS = ("mention", "thread_follow", "all")
ENTRIES = ("dm", "mention", "thread_follow", "channel", "slash_command", "button")

OWNER_PERMISSIONS = ["reply", "files", "approve", "admin"]
DEFAULT_PERMISSIONS = ["reply"]


@dataclass
class Actor:
    user: str = ""
    bot_id: str = ""
    app_id: str = ""
    is_bot: bool = False
    team_id: str = ""        # envelope team
    user_team: str = ""      # the author's own workspace, when Slack provides it
    api_app_id: str = ""     # envelope app

    @property
    def ids(self) -> set[str]:
        return {i for i in (self.user, self.bot_id, self.app_id) if i}


@dataclass
class Verdict:
    allowed: bool
    reason: str
    role: str = ""                       # owner | user | bot
    permissions: list = field(default_factory=list)
    bot_entry: dict | None = None        # matching bot_allowlist entry
    policy: dict = field(default_factory=dict)  # effective per-channel policy


def _ids(values: Any) -> set[str]:
    if not values:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


def _stricter(order: tuple[str, ...], *values: str | None) -> str:
    """Pick the strictest of the given modes; unknown values count as strictest."""
    best = 0
    for v in values:
        if v is None:
            continue
        idx = order.index(v) if v in order else len(order) - 1
        best = max(best, idx)
    return order[best]


def parse_expiry(value: Any) -> float | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0  # unparseable expiry = already expired (fail closed)


def effective_policy(cfg: dict, channel: str) -> dict:
    """Global policy tightened by channel_overrides[channel] (never loosened)."""
    ov = (cfg.get("channel_overrides") or {}).get(channel) or {}
    policy = {
        "human_access": _stricter(HUMAN_ACCESS, cfg.get("human_access", "owner_only"), ov.get("human_access")),
        "bot_access": _stricter(BOT_ACCESS, cfg.get("bot_access", "none"), ov.get("bot_access")),
        "user_denylist": _ids(cfg.get("user_denylist")) | _ids(ov.get("user_denylist")),
        "bot_denylist": _ids(cfg.get("bot_denylist")) | _ids(ov.get("bot_denylist")),
        "user_allowlist": _ids(cfg.get("user_allowlist")),
        "channel_user_allowlist": _ids(ov.get("user_allowlist")) if "user_allowlist" in ov else None,
        "channel_bot_allowlist": _ids(ov.get("bot_allowlist")) if "bot_allowlist" in ov else None,
        # Behaviour (not access): channels may choose their own trigger and loop limits,
        # but loop limits can only get stricter.
        "trigger": ov.get("trigger") or cfg.get("trigger", "mention"),
        "max_bot_turns": min(int(cfg.get("max_bot_turns", 4)),
                             int(ov.get("max_bot_turns", cfg.get("max_bot_turns", 4)))),
        "bot_cooldown_seconds": max(float(cfg.get("bot_cooldown_seconds", 10)),
                                    float(ov.get("bot_cooldown_seconds", 0))),
    }
    if policy["trigger"] not in TRIGGERS:
        policy["trigger"] = "mention"
    return policy


def match_bot_entry(entries: Iterable[dict], actor: Actor, channel: str, root_ts: str,
                    now: float) -> tuple[dict | None, str]:
    """First bot_allowlist entry whose IDs and scope match; or (None, why)."""
    why = "not in bot_allowlist"
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        want = {k: str(entry.get(k) or "") for k in ("user_id", "bot_id", "app_id")}
        if not any(want.values()):
            continue  # an entry must name at least one real ID
        have = {"user_id": actor.user, "bot_id": actor.bot_id, "app_id": actor.app_id}
        if any(v and have[k] != v for k, v in want.items()):
            continue
        chans = _ids(entry.get("channels"))
        if chans and channel not in chans:
            why = "bot_allowlist entry does not cover this channel"
            continue
        threads = {t.replace("/", ":") for t in _ids(entry.get("threads"))}
        if threads and f"{channel}:{root_ts}" not in threads:
            why = "bot_allowlist entry does not cover this thread"
            continue
        exp = parse_expiry(entry.get("expires_at"))
        if exp is not None and now >= exp:
            why = "bot_allowlist entry expired"
            continue
        return entry, "ok"
    return None, why


def check(cfg: dict, ident, actor: Actor, *, channel: str, root_ts: str = "", entry: str,
          now: float | None = None) -> Verdict:
    now = time.time() if now is None else now
    policy = effective_policy(cfg, channel)

    def refuse(reason: str) -> Verdict:
        return Verdict(False, reason, policy=policy)

    # 1. workspace + app
    if not ident.team_id or not ident.app_id:
        return refuse("bridge identity (team/app id) unknown; refusing everything")
    if actor.team_id != ident.team_id:
        return refuse(f"event from another workspace ({actor.team_id or '?'})")
    if actor.api_app_id != ident.app_id:
        return refuse(f"event for another app ({actor.api_app_id or '?'})")
    if entry not in ENTRIES:
        return refuse(f"unknown entry point {entry}")
    # 2. identity
    if not actor.ids:
        return refuse("unknown identity")
    if not actor.is_bot and actor.user_team and actor.user_team != ident.team_id:
        return refuse("user from another workspace")
    if actor.is_bot and not (actor.bot_id or actor.app_id):
        return refuse("bot without bot/app id")
    # 3. channel allowlist
    allowed_channels = _ids(cfg.get("channel_allowlist"))
    if allowed_channels and not channel.startswith("D") and channel not in allowed_channels:
        return refuse("channel not in channel_allowlist")
    # 4. denylists win
    denied = actor.ids & (policy["user_denylist"] | policy["bot_denylist"])
    if denied:
        return refuse(f"denylisted ({', '.join(sorted(denied))})")
    owner = cfg.get("owner_user_id") or ""
    # 5a. humans
    if not actor.is_bot:
        if owner and actor.user == owner:
            return Verdict(True, "owner", "owner", list(OWNER_PERMISSIONS), policy=policy)
        mode = policy["human_access"]
        if mode == "everyone":
            return Verdict(True, "human_access=everyone", "user", list(DEFAULT_PERMISSIONS), policy=policy)
        if mode == "allowlist":
            in_global = actor.user in policy["user_allowlist"]
            chan = policy["channel_user_allowlist"]
            if in_global and (chan is None or actor.user in chan):
                return Verdict(True, "user_allowlist", "user", list(DEFAULT_PERMISSIONS), policy=policy)
            return refuse("not in user_allowlist")
        return refuse("human_access=owner_only" + ("" if owner else " and no owner configured"))
    # 5b. bots
    if entry in ("slash_command", "button"):
        return refuse("bots cannot use commands or buttons")
    mode = policy["bot_access"]
    if mode == "none":
        return refuse("bot_access=none")
    chan = policy["channel_bot_allowlist"]
    if chan is not None and not (actor.ids & chan):
        return refuse("bot not in this channel's bot_allowlist")
    if mode == "all":
        return Verdict(True, "bot_access=all", "bot", list(DEFAULT_PERMISSIONS), policy=policy)
    entry_match, why = match_bot_entry(cfg.get("bot_allowlist") or [], actor, channel, root_ts, now)
    if entry_match is None:
        return refuse(why)
    return Verdict(True, f"bot_allowlist {entry_match.get('label') or ''}".strip(), "bot",
                   list(DEFAULT_PERMISSIONS), bot_entry=entry_match, policy=policy)


def validate(cfg: dict) -> list[str]:
    """Problems in access-related config (for doctor and startup logs)."""
    problems = []
    if cfg.get("human_access", "owner_only") not in HUMAN_ACCESS:
        problems.append(f"human_access must be one of {HUMAN_ACCESS} (treated as owner_only)")
    if cfg.get("bot_access", "none") not in BOT_ACCESS:
        problems.append(f"bot_access must be one of {BOT_ACCESS} (treated as none)")
    if cfg.get("trigger", "mention") not in TRIGGERS:
        problems.append(f"trigger must be one of {TRIGGERS} (treated as mention)")
    if cfg.get("human_access", "owner_only") == "owner_only" and not cfg.get("owner_user_id"):
        problems.append("human_access=owner_only but owner_user_id is unset: nobody can use the bot")
    for i, e in enumerate(cfg.get("bot_allowlist") or []):
        if not isinstance(e, dict) or not any(e.get(k) for k in ("user_id", "bot_id", "app_id")):
            problems.append(f"bot_allowlist[{i}] must name user_id, bot_id or app_id")
            continue
        exp = parse_expiry(e.get("expires_at"))
        if exp == 0.0:
            problems.append(f"bot_allowlist[{i}].expires_at is not a valid time")
        elif exp is not None and exp < time.time():
            problems.append(f"bot_allowlist[{i}] ({e.get('label') or e.get('user_id')}) has expired")
    for ch, ov in (cfg.get("channel_overrides") or {}).items():
        if not isinstance(ov, dict):
            problems.append(f"channel_overrides[{ch}] must be an object")
            continue
        for k in ("human_access", "bot_access"):
            if k in ov:
                order = HUMAN_ACCESS if k == "human_access" else BOT_ACCESS
                g = cfg.get(k, "owner_only" if k == "human_access" else "none")
                if ov[k] in order and g in order and order.index(ov[k]) < order.index(g):
                    problems.append(f"channel_overrides[{ch}].{k}={ov[k]} is looser than the global "
                                    f"{g}; overrides can only tighten, so {g} applies")
    if "access" in cfg:
        problems.append("legacy key 'access' found; run `slackctl.sh migrate-config`")
    return problems
