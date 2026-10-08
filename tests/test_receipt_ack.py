"""The bridge acknowledges accepted messages itself (👀 by default, assistant status as
fallback); the routine run posts nothing; reply.sh --op removes the ack."""

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import fakes
from fakes import BOT_USER, FakePoster, FakeResp, SlackError, env, make_bridge

import common
import slackctl


class StatusWeb(fakes.FakeWeb):
    """FakeWeb whose assistant.threads.setStatus works (others behave as FakeWeb)."""

    def __init__(self, react_error=None, status_ok=True):
        super().__init__()
        self.react_error = react_error
        self.status_ok = status_ok

    def reactions_add(self, **kw):
        self._rec("reactions_add", kw)
        if self.react_error:
            raise self.react_error
        return FakeResp(ok=True)

    def api_call(self, method, json=None):
        self._rec("api_call:" + method, json or {})
        if method == "assistant.threads.setStatus" and self.status_ok:
            return FakeResp(ok=True)
        if method == "agents.sessions.setStatus" and getattr(self, "sessions_ok", False):
            return FakeResp(ok=True, status=(json or {}).get("status"))
        return FakeResp(ok=False, error="not_authorized")


def bridge_with(web=None, scopes=None, **cfg):
    b = make_bridge(**cfg)
    if web is not None:
        b.web = web
    b.granted_scopes = scopes
    return b


def ack_meta(b, e):
    raw = b.store.meta(f"ack:{e['event_id']}")
    return json.loads(raw) if raw else None


def statuses(web):
    return web.calls_of("api_call:assistant.threads.setStatus")


class BridgeAckTests(unittest.TestCase):
    def test_dm_gets_eyes_reaction_and_nothing_is_posted(self):
        b = bridge_with()
        e = env("hello")
        self.assertEqual(b._handle_envelope(e), "queued")
        self.assertEqual(b.web.calls_of("reactions_add"),
                         [{"channel": "D0DM", "timestamp": e["event"]["ts"], "name": "eyes"}])
        self.assertEqual(ack_meta(b, e), {"kind": "reaction", "channel": "D0DM",
                                          "ts": e["event"]["ts"], "name": "eyes"})
        self.assertEqual(b.web.calls_of("chat_postMessage"), [])
        self.assertEqual(b.deliver(b.store.next_queued()), "accepted")
        payload = b.poster.payloads[0]
        self.assertEqual(payload["acknowledged"], {"by": "bridge", "mode": "reaction", "emoji": "eyes"})
        self.assertEqual(payload["handling"]["routine_run"], "silent_handoff")
        self.assertEqual(payload["handling"]["post_to_slack"], "never")
        self.assertIn("--ack-ts", payload["reply"]["command"])
        self.assertIn("--ack-ts", payload["reply"]["no_reply_command"])

    def test_reaction_also_added_in_agent_sessions(self):
        web = StatusWeb()
        web.sessions_ok = True
        b = bridge_with(web, agent_sessions=True)
        e = env("hello")
        self.assertEqual(b._handle_envelope(e), "queued")
        self.assertEqual(len(web.calls_of("reactions_add")), 1)
        self.assertEqual(statuses(web), [])  # no assistant status when the reaction worked
        payload = json.loads(b.store.next_queued()["payload"])
        self.assertIn("--session-status active", payload["reply"]["command"])
        self.assertIn("--ack-ts", payload["reply"]["command"])

    def test_channel_mention_reacts(self):
        b = bridge_with()
        e = env(f"<@{BOT_USER}> hi", channel="C0CHAN")
        self.assertEqual(b._handle_envelope(e), "queued")
        self.assertEqual(b.web.calls_of("reactions_add")[0]["channel"], "C0CHAN")

    def test_reaction_failure_falls_back_to_status_in_dm(self):
        web = StatusWeb(react_error=SlackError("missing_scope"))
        b = bridge_with(web)
        e = env("hello")
        with self.assertLogs("slack-bridge", "WARNING") as logs:
            self.assertEqual(b._handle_envelope(e), "queued")
        self.assertIn("missing_scope", "\n".join(logs.output))
        self.assertEqual(statuses(web), [{"channel_id": "D0DM", "thread_ts": e["event"]["ts"],
                                          "status": "正在处理…"}])
        self.assertEqual(ack_meta(b, e)["kind"], "status")
        self.assertEqual(b.deliver(b.store.next_queued()), "accepted")

    def test_reaction_failure_in_channel_logs_and_still_forwards(self):
        web = StatusWeb(react_error=SlackError("missing_scope"))
        b = bridge_with(web)
        e = env(f"<@{BOT_USER}> hi", channel="C0CHAN")
        with self.assertLogs("slack-bridge", "WARNING"):
            self.assertEqual(b._handle_envelope(e), "queued")
        self.assertEqual(statuses(web), [])  # no assistant status outside DMs/agent threads
        self.assertIsNone(ack_meta(b, e))
        self.assertEqual(b.deliver(b.store.next_queued()), "accepted")

    def test_missing_scope_skips_reaction_call(self):
        web = StatusWeb()
        b = bridge_with(web, scopes={"chat:write", "assistant:write"})
        e = env("hello")
        with self.assertLogs("slack-bridge", "WARNING"):
            b._handle_envelope(e)
        self.assertEqual(web.calls_of("reactions_add"), [])
        self.assertEqual(len(statuses(web)), 1)

    def test_no_fallback_without_assistant_scope(self):
        web = StatusWeb()
        b = bridge_with(web, scopes={"chat:write"})
        e = env("hello")
        with self.assertLogs("slack-bridge", "WARNING"):
            self.assertEqual(b._handle_envelope(e), "queued")
        self.assertEqual(web.calls_of("reactions_add"), [])
        self.assertEqual(statuses(web), [])

    def test_ack_errors_never_block_forwarding(self):
        web = StatusWeb(react_error=RuntimeError("boom"))
        web.api_call = mock.Mock(side_effect=RuntimeError("down"))
        b = bridge_with(web)
        e = env("hello")
        with self.assertLogs("slack-bridge", "WARNING"):
            self.assertEqual(b._handle_envelope(e), "queued")
        self.assertEqual(b.deliver(b.store.next_queued()), "accepted")

    def test_status_mode(self):
        web = StatusWeb()
        b = bridge_with(web, ack={"mode": "status", "status_text": "处理中"})
        e = env("hello")
        b._handle_envelope(e)
        self.assertEqual(statuses(web)[0]["status"], "处理中")
        self.assertEqual(web.calls_of("reactions_add"), [])
        # channels have no assistant status: the reaction is used there
        e2 = env(f"<@{BOT_USER}> hi", channel="C0CHAN")
        b._handle_envelope(e2)
        self.assertEqual(len(web.calls_of("reactions_add")), 1)

    def test_none_mode(self):
        b = bridge_with(ack={"mode": "none"})
        e = env("hello")
        b._handle_envelope(e)
        self.assertEqual(b.web.calls_of("reactions_add"), [])
        payload = json.loads(b.store.next_queued()["payload"])
        self.assertNotIn("--ack-ts", payload["reply"]["command"])
        self.assertEqual(payload["acknowledged"]["mode"], "none")

    def test_refused_and_ignored_messages_get_no_ack(self):
        b = bridge_with()
        b._handle_envelope(env("hello", user=fakes.OTHER))
        b._handle_envelope(env("not for the bot", channel="C0CHAN"))
        self.assertEqual(b.web.calls_of("reactions_add"), [])

    def test_stop_removes_the_ack(self):
        b = bridge_with()
        e = env("long task")
        b._handle_envelope(e)
        b._handle_envelope(env("stop"))
        removed = [c for c in b.web.calls_of("reactions_remove") if c["name"] == "eyes"]
        self.assertEqual(removed, [{"channel": "D0DM", "timestamp": e["event"]["ts"], "name": "eyes"}])
        self.assertIsNone(ack_meta(b, e))

    def test_legacy_keys(self):
        home = fakes.make_home(react_on_receipt=False, ack_reaction="eyes")
        self.assertEqual(common.load_config(home)["ack"]["mode"], "none")
        home = fakes.make_home(ack_reaction=":thumbsup:")
        self.assertEqual(common.ack_settings(common.load_config(home)),
                         {"mode": "reaction", "emoji": "thumbsup", "status_text": "正在处理…"})
        new, notes = common.migrate_config({"react_on_receipt": True, "ack_reaction": "eyes",
                                            "human_access": "owner_only"})
        self.assertEqual(new["ack"]["mode"], "reaction")
        self.assertNotIn("react_on_receipt", new)
        self.assertNotIn("ack_reaction", new)
        self.assertEqual(common.migrate_config(new)[1], [])
        self.assertEqual(common.ack_settings({"ack": {"mode": "bogus", "emoji": ":x:"}})["mode"], "reaction")
        self.assertTrue(common.validate_ack({"ack": {"mode": "bogus"}}))
        self.assertEqual(common.validate_ack(common.DEFAULT_CONFIG), [])

    def test_granted_scopes_header(self):
        resp = FakeResp(ok=True)
        resp.headers = {"X-OAuth-Scopes": "chat:write, reactions:write"}
        self.assertEqual(common.granted_scopes(resp), {"chat:write", "reactions:write"})
        self.assertIsNone(common.granted_scopes(FakeResp(ok=True)))


class DedicatedHandlingTests(unittest.TestCase):
    def test_dedicated_route_answers_itself(self):
        routes = {"default": "main", "channels": {"C0DEV": {
            "target": "dedicated", "webhook_url_env": "GROK_WEBHOOK_URL_DEV",
            "webhook_auth_env": "GROK_WEBHOOK_AUTH_DEV"}}}
        b = make_bridge(session_routing=routes)
        b.secrets = {"GROK_WEBHOOK_URL_DEV": "https://dev.example/h", "GROK_WEBHOOK_AUTH_DEV": "x"}
        b._handle_envelope(env(f"<@{BOT_USER}> hi", channel="C0DEV"))
        payload = json.loads(b.store.next_queued()["payload"])
        self.assertEqual(payload["handling"]["routine_run"], "answer")
        # a dedicated route that fell back to the default webhook is a main handoff
        b.secrets = {}
        b._handle_envelope(env(f"<@{BOT_USER}> again", channel="C0DEV"))
        rows = [json.loads(r["payload"]) for r in b.store.db.execute(
            "SELECT payload FROM receipts WHERE state='queued' ORDER BY rowid").fetchall()]
        self.assertEqual(rows[-1]["handling"]["routine_run"], "silent_handoff")


class ReplyClearsAckTests(unittest.TestCase):
    def reply_args(self, b, e, **kw):
        a = dict(home=str(b.home), channel="D0DM", thread_ts=None, ack_ts=e["event"]["ts"],
                 ack_reaction=None, done_reaction=None, format="markdown", op=e["event_id"],
                 no_reply=False, reason=None, session_status=None, force=False, text="answer",
                 text_file=None, allow_mention=None)
        a.update(kw)
        return argparse.Namespace(**a)

    def accepted(self, b):
        op = b.store.next_queued()
        self.assertEqual(b.deliver(op), "accepted")

    def run_reply(self, b, args, web):
        with mock.patch.object(slackctl, "web_client", return_value=web), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            rc = slackctl.cmd_reply(args)
        return rc, json.loads(out.getvalue())

    def test_final_reply_removes_reaction(self):
        b = bridge_with()
        e = env("hello")
        b._handle_envelope(e)
        self.accepted(b)
        web = StatusWeb()
        rc, out = self.run_reply(b, self.reply_args(b, e), web)
        self.assertEqual((rc, out["operation_state"]), (0, "completed"))
        self.assertEqual(web.calls_of("reactions_remove"),
                         [{"channel": "D0DM", "timestamp": e["event"]["ts"], "name": "eyes"}])
        self.assertIsNone(ack_meta(b, e))

    def test_interim_reply_keeps_reaction(self):
        b = bridge_with()
        e = env("hello")
        b._handle_envelope(e)
        self.accepted(b)
        web = StatusWeb()
        self.run_reply(b, self.reply_args(b, e, session_status="processing"), web)
        self.assertEqual(web.calls_of("reactions_remove"), [])
        self.assertIsNotNone(ack_meta(b, e))

    def test_no_reply_clears_status_fallback(self):
        web = StatusWeb(react_error=SlackError("missing_scope"))
        b = bridge_with(web)
        e = env("hello")
        with self.assertLogs("slack-bridge", "WARNING"):
            b._handle_envelope(e)
        self.accepted(b)
        web2 = StatusWeb()
        rc, out = self.run_reply(b, self.reply_args(b, e, no_reply=True), web2)
        self.assertEqual((rc, out["operation_state"]), (0, "no_reply"))
        self.assertEqual(statuses(web2), [{"channel_id": "D0DM", "thread_ts": e["event"]["ts"],
                                           "status": ""}])
        self.assertEqual(web2.calls_of("reactions_remove"), [])

    def test_reply_without_op_uses_ack_ts(self):
        b = bridge_with()
        web = StatusWeb()
        with mock.patch.object(slackctl, "web_client", return_value=web), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            slackctl.cmd_reply(argparse.Namespace(
                home=str(b.home), channel="C1", thread_ts=None, ack_ts="5.5", ack_reaction=None,
                done_reaction=None, format="plain", op=None, no_reply=False, reason=None,
                session_status=None, force=False, text="hi", text_file=None, allow_mention=None))
        self.assertEqual(web.calls_of("reactions_remove"), [{"channel": "C1", "timestamp": "5.5", "name": "eyes"}])


class DoctorAckTests(unittest.TestCase):
    def resp(self, scopes):
        r = FakeResp(ok=True)
        r.headers = {"x-oauth-scopes": ",".join(scopes)}
        return r

    def test_scope_rows(self):
        cfg = dict(common.DEFAULT_CONFIG)
        ok, _, detail = slackctl.ack_scope_check(cfg, self.resp(["reactions:write", "assistant:write"]))
        self.assertTrue(ok)
        ok, _, detail = slackctl.ack_scope_check(cfg, self.resp(["assistant:write"]))
        self.assertIsNone(ok)
        self.assertIn("Reinstall", detail)
        ok, _, _ = slackctl.ack_scope_check(dict(cfg, ack={"mode": "none"}), self.resp([]))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
