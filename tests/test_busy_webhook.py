"""A routine that is still busy with the previous message answers 400/409/429/5xx.
Those operations stay queued (in thread order) and are retried slowly."""

import unittest

from fakes import FakePoster, FakeResp, env, make_bridge

import common
import webhook


def clocked(**cfg):
    clock = {"t": 1000.0}
    b = make_bridge(**cfg)
    b.now = lambda: clock["t"]
    b.store.now = b.now
    return b, clock


def run_due(b, clock, until):
    """Deliver everything due, advancing the clock to each not_before, until `until`."""
    states = []
    while clock["t"] <= until:
        op = b.store.next_queued()
        if op is None:
            queued = b.store.list_ops(states=["queued"])
            if not queued:
                break
            clock["t"] = max(clock["t"], min(o["not_before"] for o in queued))
            continue
        states.append((round(clock["t"] - 1000), b.deliver(op)))
    return states


class ClassifyTests(unittest.TestCase):
    def test_statuses(self):
        for code in (400, 408, 409, 425, 429, 500, 502, 503, 599):
            self.assertEqual(webhook.classify_status(code).outcome, "busy", code)
        self.assertEqual(webhook.classify_status(504).outcome, "unknown")
        for code in (401, 403, 404, 410, 422):
            self.assertEqual(webhook.classify_status(code).outcome, "rejected", code)
        self.assertEqual(webhook.classify_status(204).outcome, "accepted")


class BusyRetryTests(unittest.TestCase):
    def test_schedule_then_give_up_with_chinese_notice(self):
        b, clock = clocked()
        b.poster = FakePoster(*([400] * 20))
        e = env("question")
        b._handle_envelope(e)
        states = run_due(b, clock, 10_000)
        times = [t for t, _ in states]
        self.assertEqual(times, [0, 20, 60, 140, 300, 600, 900])
        self.assertEqual([s for _, s in states], ["queued"] * 6 + ["failed"])
        op = b.store.get(e["event_id"])
        self.assertEqual(op["state"], "failed")
        posts = b.web.calls_of("chat_postMessage")
        self.assertEqual([p["text"] for p in posts], [common.DEFAULT_CONFIG["busy_failed_text"]])
        self.assertEqual(b.store.counter("webhook_failures"), 1)

    def test_no_error_text_while_waiting_and_success_later(self):
        b, clock = clocked()
        b.poster = FakePoster(409, 429, 503, "accepted")
        e = env("question")
        b._handle_envelope(e)
        states = run_due(b, clock, 10_000)
        self.assertEqual([s for _, s in states], ["queued", "queued", "queued", "accepted"])
        self.assertEqual(b.web.calls_of("chat_postMessage"), [])
        self.assertEqual(b.store.counter("webhook_failures"), 0)
        # no-session conversations get ⏳ while waiting, removed once delivered
        added = [c for c in b.web.calls_of("reactions_add") if c["name"] == "hourglass_flowing_sand"]
        removed = [c for c in b.web.calls_of("reactions_remove") if c["name"] == "hourglass_flowing_sand"]
        self.assertEqual(len(added), 1)
        self.assertEqual(len(removed), 1)

    def test_custom_schedule(self):
        b, clock = clocked(webhook_busy_retry_delays=[5], webhook_busy_retry_interval_seconds=10,
                           webhook_busy_max_seconds=30)
        b.poster = FakePoster(*([500] * 10))
        b._handle_envelope(env("q"))
        self.assertEqual([t for t, _ in run_due(b, clock, 10_000)], [0, 5, 15, 25])

    def test_credentials_rejected_immediately_with_error_text(self):
        for code in (401, 403):
            b, clock = clocked()
            b.poster = FakePoster(code)
            b._handle_envelope(env("q"))
            self.assertEqual(b.deliver(b.store.next_queued()), "failed")
            self.assertEqual([p["text"] for p in b.web.calls_of("chat_postMessage")],
                             [common.DEFAULT_CONFIG["error_text"]])
            self.assertEqual(b.store.counter("webhook_failures"), 1)

    def test_504_is_unknown_and_never_resent(self):
        b, clock = clocked()
        b.poster = FakePoster(504)
        e = env("q")
        b._handle_envelope(e)
        self.assertEqual(b.deliver(b.store.next_queued()), "unknown-result")
        clock["t"] += 10_000
        self.assertIsNone(b.store.next_queued())
        self.assertEqual(len(b.poster.payloads), 1)
        self.assertEqual(b.web.calls_of("chat_postMessage"), [])

    def test_timeout_still_unknown_and_never_resent(self):
        b, clock = clocked()
        b.poster = FakePoster("unknown")
        e = env("q")
        b._handle_envelope(e)
        self.assertEqual(b.deliver(b.store.next_queued()), "unknown-result")
        clock["t"] += 10_000
        self.assertIsNone(b.store.next_queued())
        self.assertEqual(len(b.poster.payloads), 1)


class OrderTests(unittest.TestCase):
    def test_later_message_in_thread_waits_behind_busy_one(self):
        b, clock = clocked()
        b.poster = FakePoster(400, "accepted", "accepted", "accepted")
        root = "1700009000.000100"
        first = env("<@U0GROKBOT> one", channel="C0CHAN", etype="app_mention", ts=root)
        b._handle_envelope(first)
        self.assertEqual(b.deliver(b.store.next_queued()), "queued")  # busy
        second = env("<@U0GROKBOT> two", channel="C0CHAN", etype="app_mention", thread_ts=root)
        other = env("<@U0GROKBOT> elsewhere", channel="C0CHAN", etype="app_mention")
        b._handle_envelope(second)
        b._handle_envelope(other)
        # the other thread is not blocked; the second message is
        nxt = b.store.next_queued()
        self.assertEqual(nxt["op_id"], other["event_id"])
        b.deliver(nxt)
        self.assertIsNone(b.store.next_queued())
        clock["t"] += 20
        self.assertEqual(b.store.next_queued()["op_id"], first["event_id"])
        b.deliver(b.store.next_queued())
        self.assertEqual(b.store.next_queued()["op_id"], second["event_id"])
        b.deliver(b.store.next_queued())
        order = [p["operation_id"] for p in b.poster.payloads]
        self.assertEqual(order, [first["event_id"], other["event_id"], first["event_id"], second["event_id"]])

    def test_dm_top_level_messages_keep_order(self):
        b, clock = clocked()
        b.poster = FakePoster(400)
        a = env("first")
        b._handle_envelope(a)
        b.deliver(b.store.next_queued())
        c = env("second")
        b._handle_envelope(c)
        self.assertIsNone(b.store.next_queued())
        clock["t"] += 20
        self.assertEqual(b.store.next_queued()["op_id"], a["event_id"])


class StopAndSessionTests(unittest.TestCase):
    def test_stop_cancels_queued_retry(self):
        b, clock = clocked()
        b.poster = FakePoster(400)
        e = env("q")
        b._handle_envelope(e)
        b.deliver(b.store.next_queued())
        self.assertEqual(b._handle_envelope(env("停")), "command:stop")
        self.assertEqual(b.store.get(e["event_id"])["state"], "stopped")
        clock["t"] += 10_000
        self.assertIsNone(b.store.next_queued())
        self.assertEqual(len(b.poster.payloads), 1)
        removed = [c for c in b.web.calls_of("reactions_remove") if c["name"] == "hourglass_flowing_sand"]
        self.assertEqual(len(removed), 1)

    def test_agent_session_shows_queue_status_instead_of_error(self):
        b, clock = clocked(agent_sessions=True)
        calls = []

        def api_call(method, json=None):
            calls.append((method, json or {}))
            return FakeResp(ok=True, status="processing")
        b.web.api_call = api_call
        b.poster = FakePoster(400, "accepted")
        e = env("question")
        b._handle_envelope(e)
        self.assertEqual(b.deliver(b.store.next_queued()), "queued")
        texts = [j.get("status") for m, j in calls if m == "assistant.threads.setStatus"]
        self.assertEqual(texts, ["排队中…"])
        self.assertIn(("processing"), [j.get("status") for m, j in calls if m == "agents.sessions.setStatus"])
        self.assertEqual(b.web.calls_of("chat_postMessage"), [])
        clock["t"] += 20
        self.assertEqual(b.deliver(b.store.next_queued()), "accepted")


if __name__ == "__main__":
    unittest.main()
