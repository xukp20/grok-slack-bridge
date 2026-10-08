import http.server
import socket
import threading
import time
import unittest

import fakes
from fakes import OWNER, env, make_bridge, FakePoster

import store as storemod
import webhook


class ReceiptAndDedupTests(unittest.TestCase):
    def test_dm_is_persisted_queued_and_delivered(self):
        b = make_bridge()
        e = env("hello")
        self.assertEqual(b._handle_envelope(e), "queued")
        op = b.store.next_queued()
        self.assertEqual(op["op_id"], e["event_id"])
        self.assertEqual(b.deliver(op), "accepted")
        p = b.poster.payloads[0]
        self.assertEqual(p["version"], 2)
        self.assertEqual(p["operation_id"], e["event_id"])
        self.assertIn(f"--op {e['event_id']}", p["reply"]["command"])
        self.assertIn("--no-reply", p["reply"]["no_reply_command"])
        self.assertEqual([h["to_state"] for h in b.store.history(e["event_id"])],
                         ["received", "queued", "submitted", "accepted"])

    def test_dedup_survives_restart_and_covers_channel_ts(self):
        b = make_bridge()
        e = env("<@U0GROKBOT> hi", channel="C1", etype="app_mention")
        self.assertEqual(b._handle_envelope(e), "queued")
        # same Slack retry, after a "restart" (new Bridge on the same home)
        b2 = make_bridge(home=b.home)
        self.assertEqual(b2._handle_envelope(e), "duplicate")
        # the same message delivered again as message.channels with another event_id
        e2 = env("<@U0GROKBOT> hi", channel="C1", ts=e["event"]["ts"], channel_type="channel")
        self.assertEqual(b2._handle_envelope(e2), "duplicate")

    def test_reused_id_with_different_content_is_rejected(self):
        b = make_bridge()
        e = env("first", event_id="EvSAME")
        b._handle_envelope(e)
        e2 = env("different text", event_id="EvSAME")
        self.assertEqual(b._handle_envelope(e2), "conflict")
        same_ts = env("edited text", ts=e["event"]["ts"])
        self.assertEqual(b._handle_envelope(same_ts), "conflict")
        self.assertEqual(len(b.store.list_ops()), 1)

    def test_noise_gets_no_receipt(self):
        b = make_bridge()
        for e in (env(subtype="message_changed"), env(subtype="channel_join"),
                  env(user=fakes.BOT_USER), env(user=None, bot=(fakes.BOT_ID, fakes.APP))):
            self.assertTrue(b._handle_envelope(e).startswith("noise"))
        self.assertEqual(b.store.list_ops(), [])


class DeliveryOutcomeTests(unittest.TestCase):
    def test_timeout_is_unknown_and_never_resent(self):
        b = make_bridge(poster=FakePoster("unknown"))
        b._handle_envelope(env("x"))
        op = b.store.next_queued()
        self.assertEqual(b.deliver(op), "unknown-result")
        self.assertIsNone(b.store.next_queued())
        self.assertEqual(len(b.poster.payloads), 1)

    def test_not_delivered_is_retried_then_failed(self):
        b = make_bridge(poster=FakePoster("retry", "retry", "retry"), webhook_retries=3)
        b._handle_envelope(env("x"))
        op_id = b.store.list_ops()[0]["op_id"]
        for expected in ("queued", "queued", "failed"):
            b.store.db.execute("UPDATE receipts SET not_before=0")
            self.assertEqual(b.deliver(b.store.get(op_id)), expected)
        self.assertEqual(len(b.poster.payloads), 3)

    def test_rejected_fails_immediately(self):
        b = make_bridge(poster=FakePoster("rejected"))
        b._handle_envelope(env("x"))
        self.assertEqual(b.deliver(b.store.next_queued()), "failed")


class RestartTests(unittest.TestCase):
    def test_in_flight_ops_need_reconciliation_and_are_not_replayed(self):
        b = make_bridge()
        e1, e2, e3 = env("a"), env("b"), env("c")
        for e in (e1, e2, e3):
            b._handle_envelope(e)
        b.deliver(b.store.get(e1["event_id"]))                       # accepted
        b.store.transition(e2["event_id"], "submitted")              # crashed mid-send
        # e3 stays queued (never sent)
        b2 = make_bridge(home=b.home)
        b2.start_workers()
        b2.shutdown()
        self.assertEqual(b2.store.get(e1["event_id"])["state"], "needs-reconciliation")
        self.assertEqual(b2.store.get(e2["event_id"])["state"], "needs-reconciliation")
        self.assertIn(b2.store.get(e3["event_id"])["state"], ("queued", "submitted", "accepted"))

    def test_undecided_receipt_is_decided_after_restart(self):
        b = make_bridge()
        e = env("hi")
        msg, _ = __import__("events").normalize(e, b.ident)
        b.store.record(msg.op_id, msg.fingerprint, msg_key=msg.msg_key, channel=msg.channel, ts=msg.ts,
                       thread_key=msg.thread_key(b.ident), envelope=e)
        b2 = make_bridge(home=b.home)
        b2.start_workers()
        b2.shutdown()
        self.assertNotEqual(b2.store.get(e["event_id"])["state"], "received")

    def test_reply_completes_reconciled_op(self):
        import slackctl
        b = make_bridge()
        e = env("a")
        b._handle_envelope(e)
        b.deliver(b.store.next_queued())
        b.store.recover_after_restart()
        self.assertEqual(slackctl.finish_op(b.store, e["event_id"], "completed", "replied"), "completed")
        self.assertEqual(slackctl.finish_op(b.store, e["event_id"], "completed", "again"), "completed")


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.b = make_bridge()
        self.root = "1700000100.000100"
        e = env("<@U0GROKBOT> start", channel="C1", ts=self.root, etype="app_mention")
        self.b._handle_envelope(e)
        self.b.deliver(self.b.store.next_queued())
        self.key = self.b.store.get(e["event_id"])["thread_key"]

    def cursor(self):
        return self.b.store.thread(self.key)["cursor_ts"]

    def test_cursor_advances_only_over_handled_messages(self):
        self.assertEqual(self.cursor(), self.root)
        # ignored (not mentioned) message advances the cursor
        self.b._handle_envelope(env("chatter", channel="C1", ts="1700000101.000100", thread_ts=self.root))
        self.assertEqual(self.cursor(), "1700000101.000100")
        # a forwarded message that fails blocks the cursor ...
        self.b.poster.outcomes = ["rejected"]
        bad = env("<@U0GROKBOT> q", channel="C1", ts="1700000102.000100", thread_ts=self.root)
        self.b._handle_envelope(bad)
        self.b.deliver(self.b.store.next_queued())
        self.b._handle_envelope(env("later", channel="C1", ts="1700000103.000100", thread_ts=self.root))
        self.assertEqual(self.cursor(), "1700000101.000100")
        # ... until it is explicitly resolved
        self.b.store.transition(bad["event_id"], "ignored", "operator")
        self.assertEqual(self.cursor(), "1700000103.000100")

    def test_catch_up_forwards_missed_mentions(self):
        missed = {"type": "message", "user": OWNER, "ts": "1700000105.000100", "thread_ts": self.root,
                  "text": "<@U0GROKBOT> did you see this?"}
        self.b.web.replies[("C1", self.root)] = [{"ts": self.root, "user": OWNER, "text": "start"}, missed]
        summary = self.b.catch_up()
        self.assertEqual(summary["messages"], 1)
        op = self.b.store.get("catchup:C1:1700000105.000100")
        self.assertEqual(op["state"], "queued")
        # second catch-up: nothing new
        self.assertEqual(self.b.catch_up()["messages"], 0)

    def test_failed_or_partial_read_does_not_advance(self):
        before = self.cursor()
        self.b.web.replies[("C1", self.root)] = fakes.SlackError("ratelimited")
        self.assertEqual(self.b.catch_up()["failed"], 1)
        self.assertEqual(self.b.store.thread(self.key)["catchup_ok"], 0)
        self.assertEqual(self.cursor(), before)

        def paged(kw):
            if kw.get("cursor"):
                raise fakes.SlackError("internal_error")
            return fakes.FakeResp(ok=True, has_more=True, response_metadata={"next_cursor": "p2"},
                                  messages=[{"ts": "1700000106.000100", "user": OWNER, "text": "x",
                                             "thread_ts": self.root}])
        self.b.web.replies[("C1", self.root)] = paged
        self.assertEqual(self.b.catch_up()["failed"], 1)
        self.assertIsNone(self.b.store.get("catchup:C1:1700000106.000100"))
        self.assertEqual(self.cursor(), before)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        mode = self.path.strip("/")
        if mode == "slow":
            time.sleep(1.5)
            code = 200
        else:
            code = int(mode)
        self.send_response(code)
        self.end_headers()

    def log_message(self, *a):
        pass


class WebhookOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, timeout=5):
        return webhook.post_json(self.base + path, "Bearer t", {"a": 1}, timeout)

    def test_outcomes(self):
        self.assertEqual(self.post("/200").outcome, "accepted")
        self.assertEqual(self.post("/503").outcome, "busy")
        self.assertEqual(self.post("/429").outcome, "busy")
        self.assertEqual(self.post("/400").outcome, "busy")
        self.assertEqual(self.post("/500").outcome, "busy")
        self.assertEqual(self.post("/504").outcome, "unknown")
        self.assertEqual(self.post("/401").outcome, "rejected")
        self.assertEqual(self.post("/slow", timeout=0.5).outcome, "unknown")

    def test_connection_refused_is_retry(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = webhook.post_json(f"http://127.0.0.1:{port}/x", "", {}, 2)
        self.assertEqual(r.outcome, "retry")


class StateMachineTests(unittest.TestCase):
    def test_illegal_transition_refused(self):
        st = storemod.Store(fakes.make_home() / "run" / "s.sqlite")
        st.record("op1", "fp", msg_key="C:1", channel="C", ts="1.0")
        with self.assertRaises(storemod.TransitionError):
            st.transition("op1", "completed")
        st.transition("op1", "ignored", "x")
        with self.assertRaises(storemod.TransitionError):
            st.transition("op1", "queued")


if __name__ == "__main__":
    unittest.main()
