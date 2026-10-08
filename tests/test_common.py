import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "slack-bridge" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import events  # noqa: E402
import slackctl  # noqa: E402


class EnvTests(unittest.TestCase):
    def test_missing_and_wrong_prefix(self):
        env = {"SLACK_BOT_TOKEN": "xapp-wrong", "GROK_WEBHOOK_URL": "http://x"}
        problems = common.check_env(environ=env)
        self.assertTrue(any("SLACK_BOT_TOKEN" in p and "xoxb-" in p for p in problems))
        self.assertTrue(any(p == "SLACK_APP_TOKEN: missing" for p in problems))
        self.assertTrue(any("GROK_WEBHOOK_URL" in p and "https" in p for p in problems))
        self.assertTrue(any(p == "GROK_WEBHOOK_AUTH: missing" for p in problems))

    def test_valid_env_and_no_leak(self):
        env = {"SLACK_BOT_TOKEN": "xoxb-s3cr", "SLACK_APP_TOKEN": "xapp-s3cr",
               "GROK_WEBHOOK_URL": "https://hook.example/x?token=s", "GROK_WEBHOOK_AUTH": "Bearer s"}
        self.assertEqual(common.check_env(environ=env), [])
        for name in env:
            self.assertNotIn("s3cr", common.describe_secret(name, env))

    def test_auth_header_normalization(self):
        self.assertEqual(common.normalize_auth_header("Authorization: Bearer abc"), "Bearer abc")
        self.assertEqual(common.normalize_auth_header("  Bearer abc "), "Bearer abc")

    def test_webhook_host_hides_path_and_credentials(self):
        self.assertEqual(common.webhook_host("https://u:p@api.example.com:8443/hook/123?k=v"),
                         "api.example.com")


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_secret_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cfg = common.load_config(home)
            self.assertEqual(cfg["bot_name"], "Grok Bot")
            cfg["owner_user_id"] = "U123"
            common.save_config(home, cfg)
            self.assertEqual(common.load_config(home)["owner_user_id"], "U123")
            cfg["agent_label"] = "xoxb-123"
            with self.assertRaises(ValueError):
                common.save_config(home, cfg)
            self.assertEqual(common.load_config(home)["agent_label"], "")

    def test_coercion(self):
        self.assertIs(common.coerce_config_value("react_on_receipt", "off"), False)
        self.assertEqual(common.coerce_config_value("webhook_retries", "5"), 5)
        self.assertEqual(common.coerce_config_value("ack_reaction", ":eyes:"), "eyes")
        with self.assertRaises(ValueError):
            common.coerce_config_value("human_access", "anyone")

    def test_example_config_matches_defaults(self):
        example = json.loads((ROOT / "skills/slack-bridge/config.example.json").read_text())
        self.assertEqual(example, common.DEFAULT_CONFIG)


IDENT = events.Identity(team_id="T1", app_id="A1", bot_user_id="UBOT", bot_id="BBOT")


def msg_of(envelope):
    envelope = {"event_id": "Ev1", "team_id": "T1", "api_app_id": "A1", **envelope}
    m, _ = events.normalize(envelope, IDENT)
    return m


class FilterTests(unittest.TestCase):
    def ok(self, event):
        return events.normalize({"event_id": "E", "event": event}, IDENT)[0] is not None

    def test_accepts_messages_with_a_known_author(self):
        self.assertTrue(self.ok({"type": "message", "channel": "D1", "ts": "1.1", "channel_type": "im",
                                 "user": "U1", "text": "hi"}))
        self.assertTrue(self.ok({"type": "app_mention", "channel": "C1", "ts": "1.1", "user": "U1",
                                 "text": "<@UBOT> hi"}))
        self.assertTrue(self.ok({"type": "message", "channel": "D1", "ts": "1.1", "user": "U1",
                                 "subtype": "file_share"}))
        m = events.normalize({"event_id": "E", "event": {"type": "message", "channel": "C1", "ts": "1.1",
                                                         "user": "U2", "bot_id": "B2", "app_id": "A2"}},
                             IDENT)[0]
        self.assertEqual((m.actor_type, m.bot_id, m.app_id), ("bot", "B2", "A2"))

    def test_rejects_noise(self):
        base = {"type": "message", "channel": "D1", "ts": "1.1", "channel_type": "im"}
        self.assertFalse(self.ok({**base, "user": "UBOT"}))
        self.assertFalse(self.ok({**base, "bot_id": "BBOT"}))
        self.assertFalse(self.ok({**base, "user": "U9", "bot_id": "B9", "app_id": "A1"}))
        self.assertFalse(self.ok({**base, "subtype": "message_changed"}))
        self.assertFalse(self.ok({**base, "user": "U1", "subtype": "channel_join"}))
        self.assertFalse(self.ok({**base}))
        self.assertFalse(self.ok({"type": "reaction_added", "user": "U1"}))

    def test_dedupe(self):
        d = common.Deduper(size=3)
        self.assertFalse(d.seen("Ev1", "C:1"))
        self.assertTrue(d.seen("Ev1"))
        self.assertTrue(d.seen("Ev9", "C:1"))  # same message via another event
        for i in range(5):
            d.seen(f"x{i}")
        self.assertFalse(d.seen("Ev1"))  # evicted


class PayloadTests(unittest.TestCase):
    def test_channel_mention_threads_and_owner(self):
        m = msg_of({"event": {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "1.1",
                              "text": "<@UBOT>   summarize this"}})
        cfg = dict(common.DEFAULT_CONFIG, bot_user_id="UBOT", owner_user_id="U1")
        p = common.build_payload(m, cfg, Path("/x/slack-bot"), entry="mention", user_info={"name": "jx"})
        self.assertEqual(p["text"], "summarize this")
        self.assertTrue(p["is_owner"])
        self.assertEqual(p["entry"], "mention")
        self.assertEqual(p["reply"]["thread_ts"], "1.1")
        self.assertIn("/x/slack-bot/scripts/reply.sh --op Ev1 --channel C1 --thread-ts 1.1 --ack-ts 1.1 <<'EOF'",
                      p["reply"]["command"])
        self.assertEqual(p["user_name"], "jx")
        self.assertIn("files", p["permissions"])

    def test_dm_top_level_unless_threaded(self):
        ev = {"type": "message", "channel_type": "im", "user": "U2", "channel": "D1", "ts": "2.2"}
        cfg = dict(common.DEFAULT_CONFIG, owner_user_id="U1", react_on_receipt=False)
        p = common.build_payload(msg_of({"event": ev}), cfg, Path("/h"), entry="dm")
        self.assertIsNone(p["reply"]["thread_ts"])
        self.assertFalse(p["is_owner"])
        self.assertEqual(p["permissions"], ["reply"])
        self.assertEqual(p["conversation"], "dm")
        self.assertNotIn("--ack-ts", p["reply"]["command"])
        ev["thread_ts"] = "1.0"
        self.assertEqual(common.build_payload(msg_of({"event": ev}), cfg, Path("/h"),
                                              entry="dm")["reply"]["thread_ts"], "1.0")

    def test_bot_author_is_never_owner(self):
        ev = {"type": "message", "user": "U1", "bot_id": "B5", "app_id": "A5", "channel": "C1",
              "ts": "3.3", "text": "<@UBOT> hi"}
        cfg = dict(common.DEFAULT_CONFIG, owner_user_id="U1")
        p = common.build_payload(msg_of({"event": ev}), cfg, Path("/h"), entry="mention")
        self.assertFalse(p["is_owner"])
        self.assertEqual(p["bot"], {"user_id": "U1", "bot_id": "B5", "app_id": "A5"})


class TextTests(unittest.TestCase):
    def test_chunking(self):
        text = ("para " * 50 + "\n\n") * 100
        chunks = common.chunk_text(text, limit=1000)
        self.assertTrue(all(len(c) <= 1004 for c in chunks))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(common.chunk_text("short"), ["short"])

    def test_chunking_keeps_code_fences_balanced(self):
        text = "```\n" + "\n".join(f"line {i}" for i in range(500)) + "\n```"
        for c in common.chunk_text(text, limit=800):
            self.assertEqual(c.count("```") % 2, 0)

    def test_markdown_to_mrkdwn(self):
        out = common.markdown_to_mrkdwn("# Title\n**bold** [x](https://a.b)\n- item\n```\n**raw**\n```")
        self.assertIn("*Title*", out)
        self.assertIn("*bold* <https://a.b|x>", out)
        self.assertIn("• item", out)
        self.assertIn("**raw**", out)


class AgentSessionTests(unittest.TestCase):
    CFG = {**common.DEFAULT_CONFIG, "bot_user_id": "UBOT", "owner_user_id": "UOWN"}

    def envelope(self, **event):
        base = {"type": "message", "channel_type": "im", "channel": "D1", "user": "UOWN",
                "text": "hello", "ts": "100.1"}
        base.update(event)
        return {"event_id": "Ev1", "team_id": "T1", "event": base}

    def test_session_thread_and_title(self):
        self.assertEqual(common.session_thread_ts({"ts": "1.0"}), "1.0")
        self.assertEqual(common.session_thread_ts({"ts": "2.0", "thread_ts": "1.0"}), "1.0")
        self.assertEqual(common.session_title("<@UBOT>  hi\n<https://x.io|link> there"),
                         "hi link there")
        self.assertTrue(common.session_title("x" * 300, 20).endswith("…"))
        self.assertEqual(len(common.session_title("x" * 300, 20)), 20)

    def test_context_channels(self):
        ev = {"context": {"entities": [{"type": "slack#/types/channel_id", "value": "C9"},
                                       {"type": "other", "value": "z"}]}}
        self.assertEqual(common.context_channels(ev), ["C9"])
        self.assertEqual(common.context_channels({"context": {}}), [])

    def test_payload_with_session_threads_dm_and_ends_session(self):
        env = self.envelope()
        sess = {"channel": "D1", "thread_ts": "100.1", "status": "processing"}
        p = common.build_payload(msg_of(env), self.CFG, Path("/h"), entry="dm", session=sess,
                                 viewing={"channel_ids": ["C9"], "updated_at": 1})
        self.assertEqual(p["reply"]["thread_ts"], "100.1")
        self.assertIn("--session-status active", p["reply"]["command"])
        self.assertNotIn("--ack-ts", p["reply"]["command"])
        self.assertEqual(p["agent_session"]["thread_ts"], "100.1")
        self.assertEqual(p["viewing_context"]["channel_ids"], ["C9"])

    def test_payload_without_session_unchanged(self):
        p = common.build_payload(msg_of(self.envelope()), self.CFG, Path("/h"), entry="dm")
        self.assertIsNone(p["reply"]["thread_ts"])
        self.assertIsNone(p["agent_session"])
        self.assertIn("--ack-ts", p["reply"]["command"])
        self.assertNotIn("--session-status", p["reply"]["command"])

    def test_agent_events_are_not_forwardable_messages(self):
        for t in common.AGENT_EVENTS:
            self.assertIsNone(events.normalize({"event": {"type": t, "user": "U1"}}, IDENT)[0])


class ManifestTests(unittest.TestCase):
    MDIR = ROOT / "skills/slack-bridge/manifest"

    def test_checked_in_manifests_are_current(self):
        m = slackctl.build_manifest("Grok Bot", "Chat with your Grok Bot assistant from Slack.")
        self.assertEqual(json.loads((self.MDIR / "slack-app-manifest.json").read_text()), m)
        self.assertEqual((self.MDIR / "slack-app-manifest.yaml").read_text(), slackctl.to_yaml(m) + "\n")
        self.assertTrue(m["settings"]["socket_mode_enabled"])
        self.assertTrue(m["features"]["app_home"]["messages_tab_enabled"])
        self.assertIn("assistant:write", m["oauth_config"]["scopes"]["bot"])
        self.assertIn("agent_session_stopped", m["settings"]["event_subscriptions"]["bot_events"])
        self.assertIn("app_home_opened", m["settings"]["event_subscriptions"]["bot_events"])
        self.assertLessEqual(len(m["features"]["agent_view"]["suggested_prompts"]), 4)

    def test_annotated_manifest_matches(self):
        import yaml
        annotated = yaml.safe_load((self.MDIR / "slack-app-manifest.annotated.yaml").read_text())
        self.assertEqual(annotated, json.loads((self.MDIR / "slack-app-manifest.json").read_text()))

    def test_plain_manifest_has_no_agent_features(self):
        m = slackctl.build_manifest("Grok Bot", "d", agent_view=False)
        self.assertNotIn("agent_view", m["features"])
        self.assertNotIn("assistant:write", m["oauth_config"]["scopes"]["bot"])

    def test_yaml_parses_when_pyyaml_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        for name in ("Grok Bot", "Bot: #1 'quoted'"):
            m = slackctl.build_manifest(name, "desc: with colon")
            self.assertEqual(yaml.safe_load(slackctl.to_yaml(m)), m)
        m = slackctl.build_manifest("B", "d", prompts=[{"title": "总结: 频道", "message": "- 帮我总结"}])
        self.assertEqual(yaml.safe_load(slackctl.to_yaml(m)), m)


if __name__ == "__main__":
    unittest.main()
