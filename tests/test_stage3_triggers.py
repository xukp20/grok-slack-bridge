import time
import unittest

from fakes import APP, BOT_USER, DOT_APP, DOT_BOT, DOT_USER, OTHER, OWNER, TEAM, env, make_bridge


THREAD = "1791460290.248329"
DOT = (DOT_BOT, DOT_APP)


def pilot(**kw):
    e = {"label": "dot", "user_id": DOT_USER, "bot_id": DOT_BOT, "app_id": DOT_APP,
         "threads": [f"C0CHAN:{THREAD}"], "expires_at": time.time() + 3600, "max_turns": 2}
    e.update(kw)
    return e


def bot_msg(text="ping", ts=None, thread=THREAD):
    return env(f"<@{BOT_USER}> {text}", user=DOT_USER, bot=DOT, channel="C0CHAN", thread_ts=thread,
               channel_type="channel", ts=ts)


class TriggerTests(unittest.TestCase):
    def test_mention_is_default_and_thread_follow_is_opt_in(self):
        b = make_bridge()
        # channel message without mention is ignored under trigger=mention
        e = env("hi", channel="C0CHAN", channel_type="channel", user=OWNER)
        self.assertIn("trigger=mention", b._handle_envelope(e))
        # mention still works
        self.assertEqual(b._handle_envelope(env(f"<@{BOT_USER}> hi", channel="C0CHAN",
                                                etype="app_mention")), "queued")
        # after following, trigger=thread_follow picks up later messages without a mention
        b2 = make_bridge(trigger="thread_follow")
        root = env(f"<@{BOT_USER}> start", channel="C0CHAN", etype="app_mention", ts="1700001000.000100")
        self.assertEqual(b2._handle_envelope(root), "queued")
        follow = env("follow-up", channel="C0CHAN", channel_type="channel",
                     thread_ts="1700001000.000100", user=OWNER)
        self.assertEqual(b2._handle_envelope(follow), "queued")
        # top-level (not in a thread) still needs a mention under thread_follow
        self.assertIn("trigger=thread_follow", b2._handle_envelope(
            env("top", channel="C0CHAN", channel_type="channel", user=OWNER)))

    def test_trigger_all_forwards_channel_messages(self):
        b = make_bridge(trigger="all", human_access="everyone")
        self.assertEqual(b._handle_envelope(env("anyone", channel="C0CHAN", channel_type="channel",
                                                user=OTHER)), "queued")

    def test_dms_always_count_regardless_of_trigger(self):
        b = make_bridge(trigger="mention")
        self.assertEqual(b._handle_envelope(env("dm")), "queued")


class BotLoopTests(unittest.TestCase):
    def setUp(self):
        self.b = make_bridge(bot_access="allowlist", bot_allowlist=[pilot()],
                             max_bot_turns=4, bot_cooldown_seconds=10)

    def _queue_mention(self):
        e = env(f"<@{BOT_USER}> start", channel="C0CHAN", etype="app_mention",
                ts=THREAD, thread_ts=None)
        # make the root ts = THREAD so the pilot entry's threads scope matches
        e["event"]["ts"] = THREAD
        self.assertEqual(self.b._handle_envelope(e), "queued")

    def test_max_bot_turns_and_entry_max_turns(self):
        self._queue_mention()
        # pilot max_turns=2 caps the global 4
        self.assertEqual(self.b._handle_envelope(bot_msg("1", ts="1700002001.000100")), "queued")
        self.assertEqual(self.b._handle_envelope(bot_msg("2", ts="1700002002.000100")), "queued")
        out = self.b._handle_envelope(bot_msg("3", ts="1700002003.000100"))
        self.assertIn("max_bot_turns", out)
        t = self.b.store.find_thread("C0CHAN", THREAD)
        self.assertEqual(t["bot_turns"], 2)

    def test_cooldown_delays_delivery_not_drop(self):
        clock = {"t": 1000.0}
        b = make_bridge(bot_access="allowlist", bot_allowlist=[pilot()],
                        bot_cooldown_seconds=30)
        b.now = lambda: clock["t"]
        b.store.now = b.now
        e = env(f"<@{BOT_USER}> start", channel="C0CHAN", etype="app_mention")
        e["event"]["ts"] = THREAD
        self.assertEqual(b._handle_envelope(e), "queued")
        self.assertEqual(b._handle_envelope(bot_msg("1", ts="1700003001.000100")), "queued")
        clock["t"] = 1005.0  # 5s later, still inside the 30s cooldown
        self.assertEqual(b._handle_envelope(bot_msg("2", ts="1700003002.000100")), "queued")
        ops = b.store.list_ops(states=["queued"])
        self.assertEqual(len(ops), 3)
        delayed = [o for o in ops if o["actor_type"] == "bot" and o["not_before"] > clock["t"]]
        self.assertEqual(len(delayed), 1)
        self.assertAlmostEqual(delayed[0]["not_before"], 1030.0, places=1)

    def test_owner_new_resets_bot_turns(self):
        self._queue_mention()
        self.b._handle_envelope(bot_msg("1", ts="1700004001.000100"))
        self.b._handle_envelope(bot_msg("2", ts="1700004002.000100"))
        self.assertIn("max_bot_turns", self.b._handle_envelope(bot_msg("3", ts="1700004003.000100")))
        self.assertEqual(self.b._handle_envelope(env("new", channel="C0CHAN", thread_ts=THREAD,
                                                     channel_type="channel")), "command:new")
        t = self.b.store.find_thread("C0CHAN", THREAD)
        self.assertEqual(t["bot_turns"], 0)
        self.assertEqual(t["task_id"], 2)
        self.assertEqual(t["state"], "active")
        self.assertEqual(self.b._handle_envelope(bot_msg("again", ts="1700004004.000100")), "queued")

    def test_non_owner_new_is_not_a_command(self):
        self._queue_mention()
        # OTHER saying "new" is just a normal (ignored under mention trigger) message
        out = self.b._handle_envelope(env("new", channel="C0CHAN", thread_ts=THREAD,
                                          channel_type="channel", user=OTHER))
        self.assertNotIn("command", out)
        self.assertEqual(self.b.store.find_thread("C0CHAN", THREAD)["task_id"], 1)


class StopAndRestartTests(unittest.TestCase):
    def test_stop_before_enqueue_and_before_send(self):
        b = make_bridge()
        e = env("do it")
        self.assertEqual(b._handle_envelope(e), "queued")
        # stop while queued: transition to stopped, never delivered
        self.assertEqual(b._handle_envelope(env("stop")), "command:stop")
        op = b.store.get(e["event_id"])
        self.assertEqual(op["state"], "stopped")
        self.assertIsNone(b.store.next_queued())
        # stop also blocks reply.sh
        import slackctl
        reason = slackctl.stop_gate(b.store, e["event_id"], e["event"]["channel"], None)
        self.assertIsNotNone(reason)

    def test_stop_while_submitted_finishes_as_stopped(self):
        b = make_bridge()
        e = env("do it")
        b._handle_envelope(e)
        op = b.store.next_queued()
        # simulate stop between submitted and accept
        original = b.poster

        def mid_stop(url, auth, payload, timeout):
            b.store.stop_thread(op["thread_key"], "stop mid-flight")
            return original(url, auth, payload, timeout)

        b.poster = mid_stop
        self.assertEqual(b.deliver(op), "accepted")  # webhook accepted, then marked stopped
        self.assertEqual(b.store.get(e["event_id"])["state"], "stopped")

    def test_stop_checked_before_submit(self):
        b = make_bridge()
        e = env("do it")
        b._handle_envelope(e)
        op = b.store.next_queued()
        b.store.stop_thread(op["thread_key"], "stop")
        self.assertEqual(b.deliver(op), "stopped")
        self.assertEqual(b.poster.payloads, [])

    def test_chinese_stop_words(self):
        b = make_bridge()
        for word in ("停", "停止", "别回了"):
            e = env("task")
            b._handle_envelope(e)
            self.assertEqual(b._handle_envelope(env(word)), "command:stop", word)
            self.assertEqual(b.store.get(e["event_id"])["state"], "stopped")

    def test_end_marker_and_empty_body_are_not_control(self):
        b = make_bridge()
        for text in ("[END]", "end", ""):
            # empty body is filtered as noise by normalize for messages with no author? use a space
            t = text or " "
            out = b._handle_envelope(env(t))
            self.assertNotIn("command:", out, text)

    def test_agent_view_stop_goes_through_access(self):
        b = make_bridge()
        e = env("do it")
        b._handle_envelope(e)
        # agent_session_stopped for the DM thread rooted at the message (root = thread_ts or ts)
        envelope = {"team_id": TEAM, "api_app_id": APP,
                    "event": {"type": "agent_session_stopped", "channel": "D0DM",
                              "thread_ts": e["event"]["ts"], "user": OTHER}}
        b.on_session_stopped(envelope["event"], envelope)
        # OTHER cannot stop (owner_only); op still queued
        self.assertEqual(b.store.get(e["event_id"])["state"], "queued")
        envelope["event"]["user"] = OWNER
        b.on_session_stopped(envelope["event"], envelope)
        self.assertEqual(b.store.get(e["event_id"])["state"], "stopped")

    def test_bot_threads_paused_after_restart(self):
        b = make_bridge(bot_access="allowlist", bot_allowlist=[pilot()])
        e = env(f"<@{BOT_USER}> start", channel="C0CHAN", etype="app_mention")
        e["event"]["ts"] = THREAD
        b._handle_envelope(e)
        b._handle_envelope(bot_msg("1", ts="1700005001.000100"))
        # "restart": new Bridge recovers and pauses threads with bot activity
        b2 = make_bridge(home=b.home, bot_access="allowlist", bot_allowlist=[pilot()])
        b2.start_workers()
        t = b2.store.find_thread("C0CHAN", THREAD)
        self.assertEqual(t["state"], "paused")
        # bots stay silent until a human mentions the bot again
        out = b2._handle_envelope(bot_msg("2", ts="1700005002.000100"))
        self.assertIn("paused", out)
        # owner mention reactivates
        self.assertEqual(b2._handle_envelope(env(f"<@{BOT_USER}> continue", channel="C0CHAN",
                                                 thread_ts=THREAD, channel_type="channel")),
                         "queued")
        self.assertEqual(b2.store.find_thread("C0CHAN", THREAD)["state"], "active")

    def test_no_reply_sets_thread_state(self):
        b = make_bridge()
        e = env("hi")
        b._handle_envelope(e)
        op = b.store.next_queued()
        b.deliver(op)
        import slackctl
        self.assertEqual(slackctl.finish_op(b.store, e["event_id"], "no_reply", "silent"), "no_reply")
        t = b.store.thread(op["thread_key"])
        self.assertEqual(t["state"], "no_reply")


class ButtonStopTests(unittest.TestCase):
    def test_stop_button_is_access_checked(self):
        # Stage 3 leaves buttons as "unsupported" unless a handler is registered;
        # agent_session_stopped is the Stop path. Confirm the access check still
        # refuses OTHER (covered above). This keeps the entry-point contract.
        b = make_bridge()
        from fakes import button
        self.assertEqual(b.handle_interactive(button(user=OTHER)), ["refused"])


if __name__ == "__main__":
    unittest.main()
