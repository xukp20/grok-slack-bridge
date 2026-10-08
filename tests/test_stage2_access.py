import json
import time
import unittest

import fakes
from fakes import (APP, BOT_USER, DOT_APP, DOT_BOT, DOT_USER, OTHER, OWNER, TEAM, SyncPool,
                   button, env, make_bridge, slash)

import access
import common
import events

IDENT = events.Identity(team_id=TEAM, app_id=APP, bot_user_id=BOT_USER, bot_id=fakes.BOT_ID)
THREAD = "1791460290.248329"
DOT = (DOT_BOT, DOT_APP)


def human(user=OWNER, team=TEAM, app=APP, user_team=""):
    return access.Actor(user=user, team_id=team, api_app_id=app, user_team=user_team)


def bot(user=DOT_USER, bot_id=DOT_BOT, app_id=DOT_APP):
    return access.Actor(user=user, bot_id=bot_id, app_id=app_id, is_bot=True, team_id=TEAM, api_app_id=APP)


def cfg(**kw):
    c = dict(common.DEFAULT_CONFIG, owner_user_id=OWNER, team_id=TEAM, app_id=APP)
    c.update(kw)
    return c


def dot_entry(**kw):
    e = {"label": "dot", "user_id": DOT_USER, "bot_id": DOT_BOT, "app_id": DOT_APP,
         "threads": [f"C0CHAN:{THREAD}"], "expires_at": time.time() + 3600}
    e.update(kw)
    return e


def chk(c, actor, channel="C0CHAN", root=THREAD, entry="mention", now=None):
    return access.check(c, IDENT, actor, channel=channel, root_ts=root, entry=entry, now=now)


class PolicyTests(unittest.TestCase):
    def test_default_is_owner_only_and_no_bots(self):
        c = cfg()
        self.assertTrue(chk(c, human()).allowed)
        self.assertEqual(chk(c, human()).permissions, access.OWNER_PERMISSIONS)
        self.assertFalse(chk(c, human(OTHER)).allowed)
        self.assertFalse(chk(c, bot()).allowed)

    def test_team_and_app_verified_on_every_check(self):
        c = cfg(human_access="everyone")
        self.assertIn("another workspace", chk(c, human(team="T0EVIL")).reason)
        self.assertIn("another app", chk(c, human(app="A0EVIL")).reason)
        self.assertIn("another app", chk(c, human(app="")).reason)
        self.assertIn("user from another workspace", chk(c, human(OTHER, user_team="T0EXT")).reason)
        no_ident = events.Identity(team_id=TEAM, app_id="", bot_user_id=BOT_USER, bot_id="")
        self.assertFalse(access.check(c, no_ident, human(), channel="C", entry="dm").allowed)

    def test_unknown_identity_refused(self):
        c = cfg(human_access="everyone", bot_access="all")
        self.assertEqual(chk(c, access.Actor(team_id=TEAM, api_app_id=APP)).reason, "unknown identity")
        self.assertFalse(chk(c, access.Actor(user="U1", is_bot=True, team_id=TEAM, api_app_id=APP)).allowed)

    def test_everyone_and_allowlist(self):
        self.assertTrue(chk(cfg(human_access="everyone"), human(OTHER)).allowed)
        c = cfg(human_access="allowlist", user_allowlist=[OTHER])
        self.assertTrue(chk(c, human(OTHER)).allowed)
        self.assertEqual(chk(c, human(OTHER)).permissions, ["reply"])
        self.assertFalse(chk(c, human("U0THIRD")).allowed)

    def test_denylists_win_even_over_allowlists(self):
        c = cfg(human_access="everyone", user_denylist=[OTHER])
        self.assertIn("denylisted", chk(c, human(OTHER)).reason)
        c = cfg(bot_access="all", bot_denylist=[DOT_APP])  # app id blocks every bot of that app
        self.assertIn("denylisted", chk(c, bot()).reason)
        c = cfg(bot_access="allowlist", bot_allowlist=[dot_entry()], bot_denylist=[DOT_USER])
        self.assertFalse(chk(c, bot()).allowed)

    def test_bot_allowlist_matches_real_ids_and_scope(self):
        c = cfg(bot_access="allowlist", bot_allowlist=[dot_entry()])
        self.assertTrue(chk(c, bot()).allowed)
        self.assertEqual(chk(c, bot()).role, "bot")
        self.assertNotIn("admin", chk(c, bot()).permissions)
        # an impostor bot with dot's user id but another app is refused
        self.assertFalse(chk(c, bot(app_id="A0FAKE")).allowed)
        self.assertIn("thread", chk(c, bot(), root="1.000").reason)
        self.assertFalse(chk(c, bot(), channel="C0OTHER").allowed)
        # "C/ts" spelling also works
        c2 = cfg(bot_access="allowlist", bot_allowlist=[dot_entry(threads=[f"C0CHAN/{THREAD}"])])
        self.assertTrue(chk(c2, bot()).allowed)

    def test_bot_entry_expiry_fails_closed(self):
        c = cfg(bot_access="allowlist", bot_allowlist=[dot_entry(expires_at=time.time() - 1)])
        self.assertIn("expired", chk(c, bot()).reason)
        c = cfg(bot_access="allowlist", bot_allowlist=[dot_entry(expires_at="next tuesday")])
        self.assertFalse(chk(c, bot()).allowed)
        c = cfg(bot_access="allowlist", bot_allowlist=[dot_entry(expires_at="2999-01-01T00:00:00+08:00")])
        self.assertTrue(chk(c, bot()).allowed)
        self.assertTrue(any("not a valid time" in p for p in access.validate(
            cfg(bot_allowlist=[dot_entry(expires_at="soon")]))))

    def test_bots_cannot_use_commands_or_buttons(self):
        c = cfg(bot_access="all")
        self.assertFalse(chk(c, bot(), entry="slash_command").allowed)
        self.assertFalse(chk(c, bot(), entry="button").allowed)

    def test_channel_overrides_only_tighten(self):
        c = cfg(human_access="owner_only", channel_overrides={"C0CHAN": {"human_access": "everyone"}})
        self.assertFalse(chk(c, human(OTHER)).allowed)
        self.assertTrue(any("only tighten" in p for p in access.validate(c)))
        c = cfg(human_access="everyone", channel_overrides={"C0CHAN": {"human_access": "owner_only"}})
        self.assertFalse(chk(c, human(OTHER)).allowed)
        self.assertTrue(chk(c, human(OTHER), channel="C0ELSE").allowed)
        self.assertTrue(chk(c, human()).allowed)  # owner always
        c = cfg(bot_access="all", channel_overrides={"C0CHAN": {"bot_access": "none"}})
        self.assertFalse(chk(c, bot()).allowed)
        c = cfg(bot_access="all", channel_overrides={"C0CHAN": {"bot_allowlist": ["U0SOMEONE"]}})
        self.assertFalse(chk(c, bot()).allowed)
        c = cfg(human_access="everyone", channel_overrides={"C0CHAN": {"user_denylist": [OTHER]}})
        self.assertFalse(chk(c, human(OTHER)).allowed)
        self.assertTrue(chk(c, human(OTHER), channel="C0ELSE").allowed)

    def test_channel_allowlist(self):
        c = cfg(human_access="everyone", channel_allowlist=["C0CHAN"])
        self.assertTrue(chk(c, human(OTHER)).allowed)
        self.assertFalse(chk(c, human(OTHER), channel="C0ELSE").allowed)
        self.assertTrue(chk(c, human(OTHER), channel="D0DM", entry="dm").allowed)


class MigrationTests(unittest.TestCase):
    def test_legacy_access_honoured_until_migrated(self):
        home = fakes.make_home(access="everyone")
        self.assertEqual(common.load_config(home)["human_access"], "everyone")
        self.assertTrue(any("migrate-config" in p for p in access.validate(common.load_config(home))))
        data = json.loads((home / "config.json").read_text())
        new, notes = common.migrate_config(data)
        self.assertNotIn("access", new)
        self.assertEqual(new["human_access"], "owner_only")
        self.assertEqual(new["bot_access"], "none")
        self.assertTrue(notes)
        self.assertEqual(common.migrate_config(new)[1], [])

    def test_coerce_lists_and_json(self):
        self.assertEqual(common.coerce_config_value("user_denylist", "U1, U2"), ["U1", "U2"])
        self.assertEqual(common.coerce_config_value("bot_allowlist", '[{"user_id":"U1"}]'), [{"user_id": "U1"}])
        with self.assertRaises(ValueError):
            common.coerce_config_value("bot_access", "some")
        with self.assertRaises(ValueError):
            common.coerce_config_value("channel_overrides", "[]")


class BridgeEntryPointTests(unittest.TestCase):
    def test_refused_humans_never_forwarded(self):
        b = make_bridge()
        e = env("hello", user=OTHER)
        self.assertTrue(b._handle_envelope(e).startswith("ignored:refused"))
        self.assertIsNone(b.store.next_queued())
        self.assertTrue(b.store.history(e["event_id"])[-1]["reason"].startswith("refused"))
        # one fixed deny reply in the DM, rate limited
        self.assertEqual(len(b.web.calls_of("chat_postMessage")), 1)
        b._handle_envelope(env("again", user=OTHER))
        self.assertEqual(len(b.web.calls_of("chat_postMessage")), 1)

    def test_refused_channel_mention_is_silent(self):
        b = make_bridge()
        b._handle_envelope(env(f"<@{BOT_USER}> hi", user=OTHER, channel="C0CHAN", etype="app_mention"))
        self.assertEqual(b.web.calls_of("chat_postMessage"), [])

    def test_wrong_team_or_app_envelope_refused(self):
        b = make_bridge(human_access="everyone")
        self.assertIn("another workspace", b._handle_envelope(env("x", team="T0EVIL")))
        self.assertIn("another app", b._handle_envelope(env("x", app="A0EVIL")))
        self.assertIsNone(b.store.next_queued())

    def test_bots_refused_by_default_and_allowed_by_pilot_entry(self):
        b = make_bridge()
        m = env(f"<@{BOT_USER}> hi", user=DOT_USER, bot=DOT, channel="C0CHAN", thread_ts=THREAD,
                channel_type="channel")
        self.assertEqual(b._handle_envelope(m), "ignored:refused: bot_access=none")
        b2 = make_bridge(bot_access="allowlist", bot_allowlist=[dot_entry()])
        m2 = env(f"<@{BOT_USER}> hi", user=DOT_USER, bot=DOT, channel="C0CHAN", thread_ts=THREAD,
                 channel_type="channel")
        self.assertEqual(b2._handle_envelope(m2), "queued")
        op = b2.store.next_queued()
        b2.deliver(op)
        p = b2.poster.payloads[0]
        self.assertEqual(p["actor_type"], "bot")
        self.assertFalse(p.get("is_owner"))
        self.assertEqual(p["permissions"], ["reply"])
        # never a deny reply to bots
        self.assertEqual(b.web.calls_of("chat_postMessage"), [])

    def test_owner_payload_has_owner_permissions(self):
        b = make_bridge()
        b._handle_envelope(env("hi"))
        b.deliver(b.store.next_queued())
        self.assertEqual(b.poster.payloads[0]["permissions"], access.OWNER_PERMISSIONS)

    def test_slash_command_same_check(self):
        b = make_bridge()
        b.pool = SyncPool()
        self.assertEqual(b.handle_slash(slash("x", user=OTHER)), common.DEFAULT_CONFIG["deny_message"])
        self.assertEqual(b.handle_slash(slash("x", team="T0EVIL")), common.DEFAULT_CONFIG["deny_message"])
        self.assertIsNone(b.store.next_queued())
        self.assertEqual(b.handle_slash(slash("")), common.DEFAULT_CONFIG["slash_usage_text"])
        self.assertEqual(b.handle_slash(slash("summarize")), common.DEFAULT_CONFIG["slash_ack_text"])
        op = b.store.next_queued()
        self.assertEqual(op["op_id"], "cmd:trig1")
        b.deliver(op)
        p = b.poster.payloads[0]
        self.assertEqual(p["entry"], "slash_command")
        self.assertEqual(p["reply"]["channel"], "D0" + OWNER)
        self.assertIsNone(p["reply"]["thread_ts"])
        # same trigger again (Slack retry) is a duplicate
        b.handle_slash(slash("summarize"))
        self.assertEqual(len(b.store.list_ops()), 1)

    def test_buttons_same_check(self):
        b = make_bridge()
        self.assertEqual(b.handle_interactive(button(user=OTHER)), ["refused"])
        self.assertEqual(b.handle_interactive(button(app="A0EVIL")), ["refused"])
        self.assertNotEqual(b.handle_interactive(button()), ["refused"])

    def test_thread_context_hides_refused_senders(self):
        import slackctl
        c = cfg(bot_user_id=BOT_USER)
        self.assertTrue(slackctl.context_visible(c, {"user": OWNER, "ts": "1"}, "C0CHAN", THREAD))
        self.assertTrue(slackctl.context_visible(c, {"user": BOT_USER, "bot_id": fakes.BOT_ID}, "C0CHAN", THREAD))
        self.assertFalse(slackctl.context_visible(c, {"user": OTHER}, "C0CHAN", THREAD))
        self.assertFalse(slackctl.context_visible(c, {"user": DOT_USER, "bot_id": DOT_BOT, "app_id": DOT_APP},
                                                  "C0CHAN", THREAD))


if __name__ == "__main__":
    unittest.main()
