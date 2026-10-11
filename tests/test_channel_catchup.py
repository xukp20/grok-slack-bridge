"""Channel catch-up: top-level DMs and channel mentions missed while disconnected."""

import time
import unittest

import fakes
from fakes import OWNER, OTHER, BOT_USER, DOT_USER, DOT_BOT, DOT_APP, env, make_bridge


def ts_ago(seconds, n=100):
    return f"{time.time() - seconds:.6f}"[:-6] + f"{n:06d}"


class ChannelCatchUpTests(unittest.TestCase):
    def setUp(self):
        self.b = make_bridge()
        self.b.web.conversations = [
            {"id": "D0DM", "is_im": True, "user": OWNER},
            {"id": "D0SLACKBOT", "is_im": True, "user": "USLACKBOT"},
            {"id": "C0CHAN", "is_channel": True, "is_member": True},
        ]

    def first_run(self):
        """Deploy: the very first catch-up seeds marks and forwards nothing."""
        self.b.web.history["D0DM"] = [{"type": "message", "user": OWNER, "ts": ts_ago(3 * 3600),
                                       "text": "old DM from before the deploy"}]
        s = self.b.catch_up()
        self.assertEqual(s["seeded"], 2)
        self.assertEqual(s["channel_messages"], 0)
        self.assertEqual(self.b.web.calls_of("conversations_history"), [])
        self.assertIsNone(self.b.store.next_queued())
        time.sleep(0.01)

    def queued(self):
        out = []
        while True:
            op = self.b.store.next_queued()
            if op is None:
                return out
            out.append(op)
            self.b.deliver(op)

    def test_first_run_seeds_marks_and_skips_old_messages(self):
        self.first_run()
        marks = self.b.store.channel_marks()
        self.assertEqual(set(marks), {"D0DM", "C0CHAN"})
        # second run: the pre-deploy DM is older than the mark and is not forwarded
        s = self.b.catch_up()
        self.assertEqual(s["channels"], 2)
        self.assertEqual(s["channel_messages"], 0)
        self.assertEqual(self.queued(), [])

    def test_missed_dm_and_mention_are_forwarded_once(self):
        self.first_run()
        dm_ts, mention_ts, chatter_ts = f"{time.time():.6f}", f"{time.time() + 0.001:.6f}", \
            f"{time.time() + 0.002:.6f}"
        self.b.web.history["D0DM"] = [{"type": "message", "user": OWNER, "ts": dm_ts, "text": "有新邮件吗"}]
        self.b.web.history["C0CHAN"] = [
            {"type": "message", "user": OWNER, "ts": mention_ts, "text": f"<@{BOT_USER}> ping"},
            {"type": "message", "user": OWNER, "ts": chatter_ts, "text": "just chatting"},
            {"type": "message", "user": OWNER, "ts": f"{time.time() + 0.003:.6f}", "thread_ts": mention_ts,
             "text": "a reply (thread catch-up's job)"},
        ]
        s = self.b.catch_up()
        self.assertEqual(s["channel_messages"], 3)  # DM + mention queued, chatter ignored
        ops = self.queued()
        self.assertEqual([(o["channel"], o["ts"]) for o in ops], [("D0DM", dm_ts), ("C0CHAN", mention_ts)])
        payloads = self.b.poster.payloads
        self.assertTrue(all(p["catchup"] for p in payloads))
        self.assertEqual(payloads[0]["entry"], "dm")
        self.assertEqual(payloads[1]["entry"], "mention")
        self.assertEqual(self.b.store.get(f"catchup:C0CHAN:{chatter_ts}")["state"], "ignored")
        self.assertEqual(self.b.store.channel_mark("C0CHAN"), chatter_ts)
        # again: nothing new, nothing forwarded twice
        s = self.b.catch_up()
        self.assertEqual(s["channel_messages"], 0)
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(self.b.poster.payloads), 2)

    def test_message_received_live_is_not_forwarded_again(self):
        self.first_run()
        ts = f"{time.time():.6f}"
        self.b._handle_envelope(env("hello live", channel="D0DM", ts=ts))
        self.assertEqual(len(self.queued()), 1)
        self.b.web.history["D0DM"] = [{"type": "message", "user": OWNER, "ts": ts, "text": "hello live"}]
        s = self.b.catch_up()
        self.assertEqual(s["channel_messages"], 0)
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(self.b.poster.payloads), 1)
        self.assertEqual(self.b.store.channel_mark("D0DM"), ts)

    def test_same_filters_as_live_events(self):
        self.first_run()
        t = time.time()
        self.b.web.history["D0DM"] = [{"type": "message", "user": OTHER, "ts": f"{t:.6f}", "text": "hi"}]
        self.b.web.history["C0CHAN"] = [
            {"type": "message", "user": BOT_USER, "ts": f"{t + 0.001:.6f}", "text": "my own message"},
            {"type": "message", "subtype": "channel_join", "user": OTHER, "ts": f"{t + 0.002:.6f}",
             "text": "joined"},
            {"type": "message", "user": DOT_USER, "bot_id": DOT_BOT, "app_id": DOT_APP,
             "ts": f"{t + 0.003:.6f}", "text": f"<@{BOT_USER}> from an unlisted bot"},
        ]
        self.b.catch_up()
        self.assertEqual(self.queued(), [])  # stranger refused, own/noise skipped, bot not allowlisted
        self.assertEqual(self.b.store.get(f"catchup:D0DM:{t:.6f}")["state"], "ignored")

    def test_failed_read_keeps_mark_and_retries_next_time(self):
        self.first_run()
        mark = self.b.store.channel_mark("D0DM")
        ts = f"{time.time():.6f}"
        self.b.web.history["D0DM"] = fakes.SlackError("ratelimited")
        s = self.b.catch_up()
        self.assertEqual(s["channel_failed"], 1)
        self.assertEqual(self.b.store.channel_mark("D0DM"), mark)
        self.b.web.history["D0DM"] = [{"type": "message", "user": OWNER, "ts": ts, "text": "late"}]
        self.b.catch_up()
        self.assertEqual([o["ts"] for o in self.queued()], [ts])

    def test_new_channel_uses_bounded_lookback_never_before_first_run(self):
        self.first_run()
        self.b.web.conversations.append({"id": "D0NEW", "is_im": True, "user": OWNER})
        new_ts = f"{time.time():.6f}"
        self.b.web.history["D0NEW"] = [
            {"type": "message", "user": OWNER, "ts": ts_ago(2 * 3600), "text": "before the deploy"},
            {"type": "message", "user": OWNER, "ts": new_ts, "text": "after the deploy"}]
        self.b.catch_up()
        self.assertEqual([o["ts"] for o in self.queued()], [new_ts])
        kw = [k for k in self.b.web.calls_of("conversations_history") if k["channel"] == "D0NEW"][0]
        self.assertGreaterEqual(float(kw["oldest"]), float(self.b.store.meta("catchup_channels_enabled_at")))

    def test_routes_to_dedicated_channel_and_listing_fallback(self):
        b = make_bridge(session_routing={"default": "main", "channels": {"C0DED": {
            "target": "dedicated", "webhook_url_env": "HOOK_URL_DED", "webhook_auth_env": "HOOK_AUTH_DED"}}})
        b.secrets.update({"HOOK_URL_DED": "https://ded.example/x", "HOOK_AUTH_DED": "Bearer t"})
        b.web.conversations = fakes.SlackError("missing_scope")
        s = b.catch_up()
        self.assertTrue(s["listing_failed"])
        self.assertEqual(s["seeded"], 1)  # the routed channel even though listing failed
        ts = f"{time.time() + 0.01:.6f}"
        b.web.history["C0DED"] = [{"type": "message", "user": OWNER, "ts": ts, "text": f"<@{BOT_USER}> hi"}]
        b.catch_up()
        op = b.store.next_queued()
        self.assertEqual(op["ts"], ts)
        b.deliver(op)
        self.assertEqual(b.poster.calls[-1][0], "https://ded.example/x")
        self.assertEqual(b.poster.payloads[-1]["routing"]["webhook"], "dedicated")

    def test_disabled(self):
        b = make_bridge(catchup_channels=False)
        s = b.catch_up()
        self.assertNotIn("channels", s)
        self.assertEqual(b.web.calls_of("users_conversations"), [])


if __name__ == "__main__":
    unittest.main()
