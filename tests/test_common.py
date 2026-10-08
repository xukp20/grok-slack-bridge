import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "slack-bridge" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
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
            common.coerce_config_value("access", "anyone")

    def test_example_config_matches_defaults(self):
        example = json.loads((ROOT / "skills/slack-bridge/config.example.json").read_text())
        self.assertEqual(example, common.DEFAULT_CONFIG)


class FilterTests(unittest.TestCase):
    BOT = "UBOT"

    def ok(self, event):
        return common.should_handle(event, self.BOT, "BBOT")[0]

    def test_accepts_dm_and_mention(self):
        self.assertTrue(self.ok({"type": "message", "channel_type": "im", "user": "U1", "text": "hi"}))
        self.assertTrue(self.ok({"type": "app_mention", "user": "U1", "text": "<@UBOT> hi"}))
        self.assertTrue(self.ok({"type": "message", "channel_type": "im", "user": "U1",
                                 "subtype": "file_share"}))

    def test_rejects_noise(self):
        self.assertFalse(self.ok({"type": "message", "channel_type": "im", "user": "UBOT"}))
        self.assertFalse(self.ok({"type": "message", "channel_type": "im", "bot_id": "B1", "user": "U2"}))
        self.assertFalse(self.ok({"type": "message", "channel_type": "im", "subtype": "message_changed"}))
        self.assertFalse(self.ok({"type": "message", "channel_type": "channel", "user": "U1"}))
        self.assertFalse(self.ok({"type": "message", "channel_type": "im", "user": "U1",
                                  "subtype": "channel_join"}))
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
        env = {"event_id": "Ev1", "team_id": "T1", "event": {
            "type": "app_mention", "user": "U1", "channel": "C1", "ts": "1.1",
            "text": "<@UBOT>   summarize this"}}
        cfg = dict(common.DEFAULT_CONFIG, bot_user_id="UBOT", owner_user_id="U1")
        p = common.build_payload(env, cfg, Path("/x/slack-bot"), {"name": "jx"})
        self.assertEqual(p["text"], "summarize this")
        self.assertTrue(p["is_owner"])
        self.assertEqual(p["reply"]["thread_ts"], "1.1")
        self.assertIn("/x/slack-bot/scripts/reply.sh --channel C1 --thread-ts 1.1 --ack-ts 1.1 <<'EOF'",
                      p["reply"]["command"])
        self.assertEqual(p["user_name"], "jx")

    def test_dm_top_level_unless_threaded(self):
        ev = {"type": "message", "channel_type": "im", "user": "U2", "channel": "D1", "ts": "2.2"}
        cfg = dict(common.DEFAULT_CONFIG, owner_user_id="U1", react_on_receipt=False)
        p = common.build_payload({"event": ev}, cfg, Path("/h"))
        self.assertIsNone(p["reply"]["thread_ts"])
        self.assertFalse(p["is_owner"])
        self.assertEqual(p["conversation"], "dm")
        self.assertNotIn("--ack-ts", p["reply"]["command"])
        ev["thread_ts"] = "1.0"
        self.assertEqual(common.build_payload({"event": ev}, cfg, Path("/h"))["reply"]["thread_ts"], "1.0")


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


class ManifestTests(unittest.TestCase):
    MDIR = ROOT / "skills/slack-bridge/manifest"

    def test_checked_in_manifests_are_current(self):
        m = slackctl.build_manifest("Grok Bot", "Chat with your Grok Bot assistant from Slack.")
        self.assertEqual(json.loads((self.MDIR / "slack-app-manifest.json").read_text()), m)
        self.assertEqual((self.MDIR / "slack-app-manifest.yaml").read_text(), slackctl.to_yaml(m) + "\n")
        self.assertTrue(m["settings"]["socket_mode_enabled"])
        self.assertTrue(m["features"]["app_home"]["messages_tab_enabled"])

    def test_yaml_parses_when_pyyaml_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        for name in ("Grok Bot", "Bot: #1 'quoted'"):
            m = slackctl.build_manifest(name, "desc: with colon")
            self.assertEqual(yaml.safe_load(slackctl.to_yaml(m)), m)


if __name__ == "__main__":
    unittest.main()
