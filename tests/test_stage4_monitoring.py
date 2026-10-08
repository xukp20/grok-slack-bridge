import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fakes
from fakes import BOT_USER, OTHER, OWNER, FakePoster, env, make_bridge

import bridge as bridgemod
import common
import outbox
import slackctl

Z = outbox.ZWSP


class SanitizeTests(unittest.TestCase):
    def test_broadcasts_never_ping(self):
        t = outbox.sanitize("hey <!channel> <!here|here> <!everyone> <!subteam^S123|@devs>")
        self.assertNotIn("<!", t)
        self.assertIn(f"@{Z}channel", t)
        self.assertIn(f"@{Z}here", t)
        self.assertIn(f"@{Z}@devs", t)

    def test_stray_user_mentions_rendered_inert(self):
        t = outbox.sanitize(f"<@{OWNER}> and <@U0STRANGER> and <@W0X|bob>", {OWNER})
        self.assertIn(f"<@{OWNER}>", t)
        self.assertIn(f"@{Z}U0STRANGER", t)
        self.assertIn(f"@{Z}bob", t)

    def test_reply_allowed_set(self):
        b = make_bridge()
        e = env("hi", user=OWNER)
        b._handle_envelope(e)
        cfg = dict(common.DEFAULT_CONFIG, owner_user_id=OWNER, mention_allowlist=["U0FRIEND"],
                   bot_allowlist=[{"user_id": fakes.DOT_USER}])
        allowed = slackctl.mention_allowed(cfg, b.store, e["event_id"], ["U0EXTRA,<@U0MORE>"])
        self.assertEqual(allowed, {OWNER, "U0FRIEND", fakes.DOT_USER, "U0EXTRA", "U0MORE"})


class OutboxTests(unittest.TestCase):
    def test_bounded_drop_oldest(self):
        sent = []
        ob = outbox.Outbox(sent.append, max_items=2, min_interval=0)
        for i in range(4):
            ob.put("C", None, f"m{i}")
        self.assertEqual(ob.dropped, 2)
        ob.drain()
        self.assertEqual([m["text"] for m in sent], ["m2", "m3"])

    def test_rate_limited(self):
        clock = {"t": 0.0}
        sent = []
        ob = outbox.Outbox(lambda m: sent.append(clock["t"]), max_items=10, min_interval=1.0,
                           now=lambda: clock["t"])
        import time as _t
        real_sleep = _t.sleep

        def fake_sleep(s):
            clock["t"] += s
        _t.sleep = fake_sleep
        try:
            for i in range(3):
                ob.put("C", None, "x")
            ob.drain()
        finally:
            _t.sleep = real_sleep
        self.assertEqual(len(sent), 3)
        self.assertGreaterEqual(sent[2] - sent[0], 2.0)

    def test_post_failure_does_not_raise(self):
        def boom(m):
            raise RuntimeError("x")
        ob = outbox.Outbox(boom, sync=True)
        ob.put("C", None, "x")  # no exception

    def test_bridge_notices_escape_mentions(self):
        b = make_bridge(stop_message="Stopped <!channel>")
        b._handle_envelope(env("task"))
        b._handle_envelope(env("stop"))
        text = b.web.calls_of("chat_postMessage")[-1]["text"]
        self.assertNotIn("<!channel>", text)


class ReportingTests(unittest.TestCase):
    def test_startup_report_to_configured_thread(self):
        b = make_bridge(report_channel="C0OPS", report_thread_ts="1.0")
        b.start_workers()
        b.stop_event.set()
        msgs = [m for m in b.web.calls_of("chat_postMessage") if m["channel"] == "C0OPS"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["thread_ts"], "1.0")
        self.assertIn("(re)started", msgs[0]["text"])

    def test_no_report_channel_means_log_only(self):
        b = make_bridge()
        b.start_workers()
        b.stop_event.set()
        self.assertEqual(b.web.calls_of("chat_postMessage"), [])

    def test_repeated_webhook_failures_reported_once_rate_limited(self):
        b = make_bridge(report_channel="C0OPS", report_webhook_failures=2, webhook_retries=1,
                        poster=None)
        b.poster = FakePoster("rejected", "rejected", "rejected", "accepted")
        for _ in range(3):
            b._handle_envelope(env("x"))
            b.deliver(b.store.next_queued())
        reports = [m for m in b.web.calls_of("chat_postMessage") if m["channel"] == "C0OPS"]
        self.assertEqual(len(reports), 1)
        self.assertIn("2 in a row", reports[0]["text"])
        # fixed error text to the requester's DM, never exception detail
        dm = [m for m in b.web.calls_of("chat_postMessage") if m["channel"] == "D0DM"]
        self.assertTrue(dm)
        self.assertTrue(all(m["text"] == common.DEFAULT_CONFIG["error_text"] for m in dm))
        # success resets the counter
        b._handle_envelope(env("ok"))
        b.deliver(b.store.next_queued())
        self.assertEqual(b.store.counter("webhook_failures"), 0)

    def test_disconnect_report(self):
        b = make_bridge(report_channel="C0OPS", report_disconnect_seconds=10)
        b.last_connected_at -= 60
        b.write_heartbeat()
        texts = [m["text"] for m in b.web.calls_of("chat_postMessage")]
        self.assertTrue(any("disconnected" in t for t in texts))


class CommandTests(unittest.TestCase):
    def test_help_any_allowed_human_status_owner_only(self):
        b = make_bridge(human_access="everyone")
        self.assertEqual(b._handle_envelope(env("help", user=OTHER)), "command:help")
        self.assertEqual(b.web.calls_of("chat_postMessage")[-1]["text"],
                         outbox.sanitize(common.DEFAULT_CONFIG["help_text"]))
        # status from a non-owner is just a message
        self.assertEqual(b._handle_envelope(env("status", user=OTHER)), "queued")
        self.assertEqual(b._handle_envelope(env("状态")), "command:status")
        self.assertIn("Operations:", b.web.calls_of("chat_postMessage")[-1]["text"])

    def test_commands_need_explicit_address(self):
        b = make_bridge()
        out = b._handle_envelope(env("help", channel="C0CHAN", channel_type="channel"))
        self.assertIn("trigger=mention", out)
        self.assertEqual(b._handle_envelope(env(f"<@{BOT_USER}> help", channel="C0CHAN",
                                                etype="app_mention")), "command:help")


class SecretsAndLockTests(unittest.TestCase):
    def test_take_secrets_scrubs_environment(self):
        names = ("SB_TEST_A", "SB_TEST_B")
        os.environ.update({n: "value" for n in names})
        got = common.take_secrets(names)
        self.assertEqual(got, {n: "value" for n in names})
        for n in names:
            self.assertNotIn(n, os.environ)
        out = subprocess.run([sys.executable, "-c", "import os;print(os.environ.get('SB_TEST_A'))"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "None")

    def test_single_instance_lock(self):
        home = Path(tempfile.mkdtemp())
        first = bridgemod.acquire_instance_lock(home)
        self.assertIsNotNone(first)
        code = ("import sys; sys.path.insert(0, %r); import bridge; "
                "print(bridge.acquire_instance_lock(__import__('pathlib').Path(%r)) is None)"
                % (str(fakes.SCRIPTS), str(home)))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)
        first.close()

    def test_bridge_payload_and_logs_never_contain_secrets(self):
        b = make_bridge()
        b.secrets = {"SLACK_BOT_TOKEN": "xoxb-should-not-leak"}
        b._handle_envelope(env("hi"))
        b.deliver(b.store.next_queued())
        self.assertNotIn("xoxb-", json.dumps(b.poster.payloads))


class HealthTests(unittest.TestCase):
    def test_health_separates_process_and_tasks(self):
        b = make_bridge()
        b._handle_envelope(env("hi"))
        op = b.store.next_queued()
        b.store.transition(op["op_id"], "submitted", "x")
        b.store.recover_after_restart()
        h = slackctl.health(b.home)
        self.assertFalse(h["process"]["running"])
        self.assertEqual(h["tasks"]["needs_decision"], [op["op_id"]])

    def test_download_detects_html_login_page(self):
        self.assertTrue(slackctl.looks_like_login_page("text/html; charset=utf-8",
                                                        b"<!DOCTYPE html><html>sign in"))
        self.assertFalse(slackctl.looks_like_login_page("image/png", b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
