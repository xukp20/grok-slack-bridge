"""Session routing: DMs and unconfigured channels go to the main conversation via
the default webhook; a channel may be routed to a dedicated agent's webhook."""

import json
import unittest

from fakes import BOT_USER, FakePoster, env, make_bridge, make_home

import common

DEV = "C0DEV"
ROUTES = {"default": "main", "channels": {
    DEV: {"target": "dedicated", "label": "dev bot", "webhook_url_env": "GROK_WEBHOOK_URL_DEV",
          "webhook_auth_env": "GROK_WEBHOOK_AUTH_DEV"}}}
DEV_SECRETS = {"GROK_WEBHOOK_URL_DEV": "https://dev.example/hook", "GROK_WEBHOOK_AUTH_DEV": "dev-secret"}


def mention(channel, text="hi"):
    return env(f"<@{BOT_USER}> {text}", channel=channel)


def routed_bridge(secrets=DEV_SECRETS, **cfg):
    cfg.setdefault("session_routing", ROUTES)
    b = make_bridge(**cfg)
    b.secrets = dict(secrets)
    b.webhook_auth = "Bearer main-secret"
    return b


def deliver_one(b, envelope):
    assert b._handle_envelope(envelope) == "queued"
    op = b.store.next_queued()
    return b.deliver(op)


class RouteForTests(unittest.TestCase):
    def test_defaults(self):
        r = common.route_for(common.DEFAULT_CONFIG, "D0DM")
        self.assertEqual(r, {"target": "main", "busy_policy": "interrupt_merge", "source": "default",
                             "label": "", "webhook": "default"})

    def test_dedicated_with_and_without_env(self):
        cfg = dict(common.DEFAULT_CONFIG, session_routing=ROUTES)
        r = common.route_for(cfg, DEV, DEV_SECRETS)
        self.assertEqual((r["target"], r["webhook"], r["source"], r["label"]),
                         ("dedicated", "dedicated", "channel", "dev bot"))
        r = common.route_for(cfg, DEV, {})
        self.assertEqual((r["target"], r["webhook"]), ("main", "default"))
        self.assertIn("GROK_WEBHOOK_URL_DEV", r["fallback"])
        r = common.route_for(cfg, DEV, {"GROK_WEBHOOK_URL_DEV": "http://plain", "GROK_WEBHOOK_AUTH_DEV": "x"})
        self.assertEqual(r["webhook"], "default")

    def test_busy_policy_global_and_per_channel(self):
        routes = {"channels": {"C1": {"target": "main", "busy_policy": "queue"}}}
        cfg = dict(common.DEFAULT_CONFIG, session_routing=routes)
        self.assertEqual(common.route_for(cfg, "C1")["busy_policy"], "queue")
        self.assertEqual(common.route_for(cfg, "C2")["busy_policy"], "interrupt_merge")
        cfg["busy_policy"] = "queue"
        self.assertEqual(common.route_for(cfg, "C2")["busy_policy"], "queue")
        cfg["busy_policy"] = "bogus"
        self.assertEqual(common.route_for(cfg, "C2")["busy_policy"], "interrupt_merge")

    def test_env_names(self):
        cfg = dict(common.DEFAULT_CONFIG, session_routing=ROUTES)
        self.assertEqual(common.route_env_names(cfg), ["GROK_WEBHOOK_URL_DEV", "GROK_WEBHOOK_AUTH_DEV"])
        self.assertEqual(common.route_env_names(common.DEFAULT_CONFIG), [])


class ValidationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(common.validate_routing(dict(common.DEFAULT_CONFIG, session_routing=ROUTES)), [])
        self.assertEqual(common.validate_routing(common.DEFAULT_CONFIG), [])

    def test_problems(self):
        bad = {"default": "dedicated", "channels": {
            "C1": {"target": "dedicated", "webhook_url_env": "https://x.example/hook",
                   "webhook_auth_env": "GROK_WEBHOOK_AUTH"},
            "C2": {"target": "elsewhere", "busy_policy": "drop"}}}
        problems = common.validate_routing({"session_routing": bad, "busy_policy": "later"})
        text = "\n".join(problems)
        for needle in ("default must be 'main'", "busy_policy must be", "C1].webhook_url_env must be an env var NAME",
                       "reuses GROK_WEBHOOK_AUTH", "C2].target", "C2].busy_policy"):
            self.assertIn(needle, text)

    def test_save_config_refuses_values_in_env_fields(self):
        home = make_home()
        cfg = {"session_routing": {"channels": {"C1": {"target": "dedicated",
               "webhook_url_env": "https://x.example/hook", "webhook_auth_env": "A_B"}}}}
        with self.assertRaises(ValueError):
            common.save_config(home, cfg)
        cfg["session_routing"]["channels"]["C1"].update(webhook_url_env="URL_ENV", webhook_auth_env="Bearer abc")
        with self.assertRaises(ValueError):
            common.save_config(home, cfg)
        cfg["session_routing"]["channels"]["C1"]["webhook_auth_env"] = "AUTH_ENV"
        common.save_config(home, cfg)
        self.assertNotIn("Bearer", (home / "config.json").read_text())

    def test_migrate_adds_defaults(self):
        new, notes = common.migrate_config({"human_access": "owner_only"})
        self.assertEqual(new["session_routing"], {"default": "main", "channels": {}})
        self.assertEqual(new["busy_policy"], "interrupt_merge")
        self.assertTrue(any("session_routing" in n for n in notes))
        again, notes2 = common.migrate_config(new)
        self.assertEqual(notes2, [])

    def test_coerce(self):
        self.assertEqual(common.coerce_config_value("busy_policy", "queue"), "queue")
        with self.assertRaises(ValueError):
            common.coerce_config_value("busy_policy", "drop")
        self.assertEqual(common.coerce_config_value("session_routing", json.dumps(ROUTES)), ROUTES)


class BridgeRoutingTests(unittest.TestCase):
    def test_dm_goes_to_main_via_default_webhook(self):
        b = routed_bridge()
        self.assertEqual(deliver_one(b, env("hello")), "accepted")
        self.assertEqual(b.poster.calls, [("https://hook.example/x", "Bearer main-secret")])
        r = b.poster.payloads[0]["routing"]
        self.assertEqual((r["target"], r["busy_policy"], r["source"], r["webhook"]),
                         ("main", "interrupt_merge", "default", "default"))
        self.assertIn("--op", b.poster.payloads[0]["reply"]["command"])

    def test_unconfigured_channel_uses_default(self):
        b = routed_bridge()
        deliver_one(b, mention("C0OTHER"))
        self.assertEqual(b.poster.calls[0][0], "https://hook.example/x")
        self.assertEqual(b.poster.payloads[0]["routing"]["target"], "main")

    def test_dedicated_channel_uses_its_webhook(self):
        b = routed_bridge()
        deliver_one(b, mention(DEV))
        url, auth = b.poster.calls[0]
        self.assertEqual(url, "https://dev.example/hook")
        self.assertEqual(auth, common.normalize_auth_header("dev-secret"))
        r = b.poster.payloads[0]["routing"]
        self.assertEqual((r["target"], r["webhook"], r["label"]), ("dedicated", "dedicated", "dev bot"))
        self.assertEqual(r["webhook_url_env"], "GROK_WEBHOOK_URL_DEV")  # name only
        blob = json.dumps(b.poster.payloads[0])
        self.assertNotIn("dev-secret", blob)
        self.assertNotIn("dev.example", blob)

    def test_missing_dedicated_env_falls_back_to_main(self):
        b = routed_bridge(secrets={})
        with self.assertLogs("slack-bridge", level="WARNING"):
            deliver_one(b, mention(DEV))
        self.assertEqual(b.poster.calls[0][0], "https://hook.example/x")
        r = b.poster.payloads[0]["routing"]
        self.assertEqual((r["target"], r["webhook"]), ("main", "default"))
        self.assertIn("fallback", r)

    def test_queue_policy_passed_through(self):
        b = routed_bridge(busy_policy="queue")
        deliver_one(b, env("hello"))
        self.assertEqual(b.poster.payloads[0]["routing"]["busy_policy"], "queue")

    def test_busy_retry_keeps_dedicated_target(self):
        b = routed_bridge()
        b.poster = FakePoster(429, "accepted")
        clock = {"t": 1000.0}
        b.now = lambda: clock["t"]
        b.store.now = b.now
        e = mention(DEV)
        self.assertEqual(deliver_one(b, e), "queued")
        clock["t"] += 25
        op = b.store.next_queued()
        self.assertEqual(op["op_id"], e["event_id"])
        self.assertEqual(b.deliver(op), "accepted")
        self.assertEqual([c[0] for c in b.poster.calls], ["https://dev.example/hook"] * 2)

    def test_heartbeat_lists_dedicated_hosts_only(self):
        b = routed_bridge()
        self.assertEqual(b.dedicated_hosts(), {DEV: "dev.example"})
        b.secrets = {}
        self.assertEqual(b.dedicated_hosts(), {})


if __name__ == "__main__":
    unittest.main()
