"""Offline fakes for Slack and the webhook (no network, no tokens)."""

import itertools
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "slack-bridge" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import webhook  # noqa: E402

TEAM = "T0TEAM"
APP = "A0GROK"
BOT_USER = "U0GROKBOT"
BOT_ID = "B0GROK"
OWNER = "U0OWNER"
OTHER = "U0OTHER"
DOT_USER, DOT_BOT, DOT_APP = "U0DOT", "B0DOT", "A0DOT"


class FakeResp(dict):
    @property
    def data(self):
        return dict(self)


class SlackError(Exception):
    def __init__(self, error):
        super().__init__(error)
        self.response = type("R", (), {"data": {"ok": False, "error": error}})()


class FakeWeb:
    def __init__(self):
        self.calls = []
        self.replies = {}           # (channel, root_ts) -> list of messages or Exception
        self.ts = itertools.count(1)

    def _rec(self, name, kw):
        self.calls.append((name, kw))

    def calls_of(self, name):
        return [kw for n, kw in self.calls if n == name]

    def auth_test(self):
        return FakeResp(ok=True, user_id=BOT_USER, bot_id=BOT_ID, team_id=TEAM, team="Team",
                        user="grok_bot", url="https://x.slack.com/")

    def bots_info(self, bot):
        return FakeResp(ok=True, bot={"id": bot, "app_id": APP, "user_id": BOT_USER})

    def users_info(self, user):
        return FakeResp(ok=True, user={"id": user, "name": user.lower(), "profile": {}})

    def reactions_add(self, **kw):
        self._rec("reactions_add", kw)
        return FakeResp(ok=True)

    def reactions_remove(self, **kw):
        self._rec("reactions_remove", kw)
        return FakeResp(ok=True)

    def chat_postMessage(self, **kw):
        self._rec("chat_postMessage", kw)
        return FakeResp(ok=True, ts=f"9{next(self.ts):09d}.000100", channel=kw.get("channel"))

    def conversations_open(self, users):
        self._rec("conversations_open", {"users": users})
        return FakeResp(ok=True, channel={"id": "D0" + users})

    def conversations_replies(self, **kw):
        self._rec("conversations_replies", kw)
        data = self.replies.get((kw["channel"], kw["ts"]), [])
        if isinstance(data, Exception):
            raise data
        if callable(data):
            return data(kw)
        return FakeResp(ok=True, messages=data, has_more=False)

    def api_call(self, method, json=None):
        self._rec("api_call:" + method, json or {})
        return FakeResp(ok=False, error="not_authorized")  # no agent view by default


class FakePoster:
    """Records payloads; returns scripted outcomes (default accepted)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.payloads = []

    def __call__(self, url, auth, payload, timeout):
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0) if self.outcomes else "accepted"
        status = {"accepted": 200, "rejected": 401}.get(outcome, 0)
        return webhook.Result(outcome, status, outcome)


def make_home(**cfg):
    home = Path(tempfile.mkdtemp(prefix="bridge-test-"))
    base = {"owner_user_id": OWNER, "team_id": TEAM, "app_id": APP, "bot_user_id": BOT_USER,
            "agent_sessions": False}
    base.update(cfg)
    (home / "config.json").write_text(json.dumps(base))
    return home


def make_bridge(home=None, poster=None, **cfg):
    import bridge as bridgemod
    home = home or make_home(**cfg)
    web = FakeWeb()
    b = bridgemod.Bridge(home, web=web, poster=poster or FakePoster())
    b.ident.team_id, b.ident.app_id, b.ident.bot_user_id, b.ident.bot_id = TEAM, APP, BOT_USER, BOT_ID
    b.webhook_url = "https://hook.example/x"
    return b


_ids = itertools.count(1)


def env(text="hi", user=OWNER, channel="D0DM", ts=None, thread_ts=None, etype="message",
        channel_type=None, bot=None, event_id=None, team=TEAM, app=APP, subtype=None):
    ts = ts or f"17000000{next(_ids):02d}.000100"
    event = {"type": etype, "channel": channel, "ts": ts, "text": text}
    if user:
        event["user"] = user
    if thread_ts:
        event["thread_ts"] = thread_ts
    if channel_type or etype == "message":
        event["channel_type"] = channel_type or ("im" if channel.startswith("D") else "channel")
    if bot:
        event["bot_id"], event["app_id"] = bot
        event["bot_profile"] = {"id": bot[0], "app_id": bot[1]}
    if subtype:
        event["subtype"] = subtype
    return {"event_id": event_id or f"Ev{next(_ids):06d}", "team_id": team, "api_app_id": app,
            "event": event}


class SyncPool:
    """Runs submitted work inline (deterministic tests)."""

    def submit(self, fn, *a, **kw):
        fn(*a, **kw)

    def shutdown(self, *a, **kw):
        pass


def slash(text="do it", user=OWNER, channel="C0CHAN", team=TEAM, app=APP, trigger="trig1"):
    return {"team_id": team, "api_app_id": app, "user_id": user, "channel_id": channel,
            "command": "/grok", "text": text, "trigger_id": trigger}


def button(action_id="bridge:stop", user=OWNER, channel="C0CHAN", ts="1700000000.000100",
           team=TEAM, app=APP, value=""):
    return {"type": "block_actions", "team": {"id": team}, "api_app_id": app,
            "user": {"id": user, "team_id": team}, "channel": {"id": channel},
            "message": {"ts": ts}, "actions": [{"action_id": action_id, "value": value}]}
