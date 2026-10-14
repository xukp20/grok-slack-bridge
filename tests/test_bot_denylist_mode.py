"""bot_access=denylist (default allow, deny wins) and unlimited bot turns by default."""
import time
import unittest

from fakes import BOT_USER, DOT_APP, DOT_BOT, DOT_USER, OWNER, env, make_bridge
from test_stage2_access import bot, cfg, chk, dot_entry

import access
import common

THREAD = "1791460290.248329"


def other_bot():
    return bot(user="U0OTHERBOT", bot_id="B0OTHER", app_id="A0OTHER")


def bot_msg(text, ts, user=DOT_USER, ids=(DOT_BOT, DOT_APP), mention=True):
    return env((f"<@{BOT_USER}> " if mention else "") + text, user=user, bot=ids, channel="C0CHAN",
               thread_ts=THREAD, channel_type="channel", ts=ts)


class DenylistModeAccess(unittest.TestCase):
    def test_denylist_allows_any_bot_without_registration(self):
        c = cfg(bot_access="denylist")
        self.assertTrue(chk(c, bot()).allowed)
        self.assertTrue(chk(c, other_bot()).allowed)
        self.assertEqual(chk(c, other_bot()).reason, "bot_access=denylist")

    def test_deny_by_user_bot_or_app_id_wins(self):
        for denied in (DOT_USER, DOT_BOT, DOT_APP):
            c = cfg(bot_access="denylist", bot_denylist=[denied])
            self.assertIn("denylisted", chk(c, bot()).reason)
            self.assertTrue(chk(c, other_bot()).allowed)
        # an allowlist entry does not override the denylist
        c = cfg(bot_access="denylist", bot_allowlist=[dot_entry()], bot_denylist=[DOT_APP])
        self.assertFalse(chk(c, bot()).allowed)

    def test_allowlist_entry_still_attached_but_scope_not_required(self):
        c = cfg(bot_access="denylist", bot_allowlist=[dot_entry(threads=["C0ELSE:1.2"])])
        v = chk(c, bot())
        self.assertTrue(v.allowed)
        c = cfg(bot_access="denylist", bot_allowlist=[dot_entry()])
        self.assertEqual(chk(c, bot()).bot_entry["label"], "dot")

    def test_all_is_legacy_alias(self):
        self.assertEqual(access.effective_policy(cfg(bot_access="all"), "C0CHAN")["bot_access"], "denylist")
        self.assertTrue(chk(cfg(bot_access="all"), other_bot()).allowed)
        self.assertEqual(common.coerce_config_value("bot_access", "denylist"), "denylist")
        self.assertEqual(common.coerce_config_value("max_bot_turns", "0"), 0)

    def test_channel_override_can_tighten_and_unknown_fails_closed(self):
        c = cfg(bot_access="denylist", channel_overrides={"C0CHAN": {"bot_access": "none"}})
        self.assertFalse(chk(c, bot()).allowed)
        c = cfg(bot_access="denylist", channel_overrides={"C0CHAN": {"bot_access": "bogus"}})
        self.assertFalse(chk(c, bot()).allowed)
        c = cfg(bot_access="denylist", channel_overrides={"C0CHAN": {"bot_denylist": [DOT_BOT]}})
        self.assertFalse(chk(c, bot()).allowed)
        self.assertTrue(chk(c, bot(), channel="C0OTHER").allowed)
        c = cfg(bot_access="allowlist", channel_overrides={"C0CHAN": {"bot_access": "denylist"}})
        self.assertEqual(access.effective_policy(c, "C0CHAN")["bot_access"], "allowlist")
        self.assertTrue(any("looser" in p for p in access.validate(c)))

    def test_validate_accepts_denylist(self):
        self.assertEqual([p for p in access.validate(cfg(bot_access="denylist")) if "bot_access" in p], [])
        self.assertTrue(any("bot_access" in p for p in access.validate(cfg(bot_access="maybe"))))


class UnlimitedTurns(unittest.TestCase):
    def test_default_turn_limit_is_unlimited(self):
        self.assertEqual(common.DEFAULT_CONFIG["max_bot_turns"], 0)
        self.assertEqual(access.effective_policy(cfg(), "C0CHAN")["max_bot_turns"], 0)
        self.assertEqual(access.effective_policy(
            cfg(max_bot_turns=0, channel_overrides={"C0CHAN": {"max_bot_turns": 3}}), "C0CHAN")["max_bot_turns"], 3)
        self.assertEqual(access.effective_policy(cfg(max_bot_turns=5), "C0CHAN")["max_bot_turns"], 5)
        self.assertEqual(access.effective_policy(cfg(max_bot_turns=None), "C0CHAN")["max_bot_turns"], 0)

    def test_many_bot_turns_forwarded_and_mention_still_required(self):
        b = make_bridge(bot_access="denylist", bot_cooldown_seconds=0)
        root = env(f"<@{BOT_USER}> start", channel="C0CHAN", etype="app_mention", ts=THREAD, thread_ts=None)
        self.assertEqual(b._handle_envelope(root), "queued")
        for i in range(10):
            out = b._handle_envelope(bot_msg(str(i), ts=f"17000090{i:02d}.000100"))
            self.assertNotIn("max_bot_turns", str(out))
        # an unknown, unregistered bot is allowed too, but only when it @mentions the bot
        other = ("B0OTHER", "A0OTHER")
        self.assertIn("trigger=mention", str(b._handle_envelope(
            bot_msg("no mention", ts="1700009500.000100", user="U0OTHERBOT", ids=other, mention=False))))
        self.assertNotIn("refused", str(b._handle_envelope(
            bot_msg("hi", ts="1700009501.000100", user="U0OTHERBOT", ids=other))))
        # a denylisted bot is refused even with a mention
        b2 = make_bridge(bot_access="denylist", bot_denylist=[DOT_APP], bot_cooldown_seconds=0)
        self.assertIn("denylisted", str(b2._handle_envelope(bot_msg("x", ts="1700009600.000100"))))


if __name__ == "__main__":
    unittest.main()
