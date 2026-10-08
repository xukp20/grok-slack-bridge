"""`slackctl route add|remove` and scripts/add-channel-route.sh: route one channel
to a dedicated agent's webhook by env var NAME, with a backup, never echoing
secret values and never restarting unless asked."""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fakes  # noqa: F401  (puts the scripts directory on sys.path)

import common
import slackctl

SCRIPT = fakes.SCRIPTS / "add-channel-route.sh"
CH = "C0MYBOTS1"
URL_ENV, AUTH_ENV = "GROK_WEBHOOK_URL_TEST", "GROK_WEBHOOK_AUTH_TEST"
URL_VAL, AUTH_VAL = "https://hooks.example/secret-path-123", "Bearer s3cr3t-value-xyz"
GOOD_ENV = {URL_ENV: URL_VAL, AUTH_ENV: AUTH_VAL}


def make_home(cfg=None):
    home = Path(tempfile.mkdtemp(prefix="sb-route-"))
    (home / "config.json").write_text(json.dumps(cfg if cfg is not None else
                                                 {"owner_user_id": "U0OWNER"}), encoding="utf-8")
    return home


def run(home, *argv, environ=None):
    """Run slackctl in-process; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    env = {k: v for k, v in os.environ.items() if k not in GOOD_ENV}
    env.update(environ or {})
    code = 0
    with mock.patch.dict(os.environ, env, clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = slackctl.main(["--home", str(home), "route", *argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def raw(home):
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def backups(home):
    return sorted(home.glob("config.json.bak-*"))


ADD = ("add", "--channel", CH, "--label", "Grok Bot #my-bots", "--url-env", URL_ENV, "--auth-env", AUTH_ENV)


class RouteAddTests(unittest.TestCase):
    def test_add_dedicated_backs_up_and_never_prints_values(self):
        home = make_home()
        code, out, err = run(home, *ADD, environ=GOOD_ENV)
        self.assertEqual(code, 0, err)
        entry = raw(home)["session_routing"]["channels"][CH]
        self.assertEqual(entry, {"target": "dedicated", "label": "Grok Bot #my-bots",
                                 "webhook_url_env": URL_ENV, "webhook_auth_env": AUTH_ENV})
        self.assertEqual(raw(home)["session_routing"]["default"], "main")
        self.assertNotIn("human_access", raw(home), "must not materialise defaults into config.json")
        [bak] = backups(home)
        self.assertEqual(json.loads(bak.read_text()), {"owner_user_id": "U0OWNER"})
        self.assertEqual(stat.S_IMODE(bak.stat().st_mode), 0o600)
        for secret in (URL_VAL, AUTH_VAL, "secret-path", "s3cr3t"):
            self.assertNotIn(secret, out + err)
        self.assertIn(f"env {URL_ENV}: set", out)
        self.assertIn("restart.sh", out)          # told to restart, did not restart
        r = common.route_for(common.load_config(home), CH, GOOD_ENV)
        self.assertEqual((r["target"], r["webhook"]), ("dedicated", "dedicated"))

    def test_secret_looking_names_refused_without_echo(self):
        home = make_home()
        for bad in (URL_VAL, AUTH_VAL, "xoxb-123-456", "xapp-1-abc"):
            for flag in ("--url-env", "--auth-env"):
                argv = list(ADD)
                argv[argv.index(flag) + 1] = bad
                code, out, err = run(home, *argv, environ=GOOD_ENV)
                self.assertEqual(code, 2)
                self.assertIn("looks like a secret value", err)
                self.assertNotIn(bad, out + err)
        self.assertNotIn("session_routing", raw(home))
        self.assertEqual(backups(home), [])

    def test_bad_names_and_channels(self):
        home = make_home()
        cases = [
            ("--url-env", "lower_case"), ("--url-env", "GROK_WEBHOOK_URL"),   # reuses the main route's var
            ("--auth-env", URL_ENV),                                          # same var twice
        ]
        for flag, value in cases:
            argv = list(ADD)
            argv[argv.index(flag) + 1] = value
            code, _, err = run(home, *argv, environ=GOOD_ENV)
            self.assertEqual(code, 2, (flag, value, err))
        for ch in ("D0DMCHANNEL", "general", "C1"):
            argv = list(ADD)
            argv[argv.index("--channel") + 1] = ch
            self.assertEqual(run(home, *argv, environ=GOOD_ENV)[0], 2)
        argv = list(ADD)
        argv[argv.index("--label") + 1] = "see https://hooks.example/x"
        self.assertEqual(run(home, *argv, environ=GOOD_ENV)[0], 2)
        self.assertNotIn("session_routing", raw(home))

    def test_missing_env_refused_unless_allowed(self):
        home = make_home()
        code, out, err = run(home, *ADD)
        self.assertEqual(code, 2)
        self.assertIn(f"env {URL_ENV}: missing", out)
        self.assertNotIn("session_routing", raw(home))
        code, out, err = run(home, *ADD, environ={URL_ENV: "http://plain.example", AUTH_ENV: "x"})
        self.assertEqual(code, 2)
        self.assertIn("not an https", out)
        code, out, err = run(home, *ADD, "--allow-missing-env")
        self.assertEqual(code, 0, err)
        r = common.route_for(common.load_config(home), CH, {})
        self.assertEqual((r["target"], r["source"]), ("main", "channel"))
        self.assertIn("fallback", r)

    def test_replace_required_for_a_different_entry(self):
        home = make_home()
        self.assertEqual(run(home, *ADD, environ=GOOD_ENV)[0], 0)
        self.assertEqual(run(home, *ADD, environ=GOOD_ENV)[0], 0)     # identical: no-op
        self.assertEqual(len(backups(home)), 1)
        code, _, err = run(home, *ADD, "--busy-policy", "queue", environ=GOOD_ENV)
        self.assertEqual(code, 2)
        self.assertIn("--replace", err)
        code, _, err = run(home, *ADD, "--busy-policy", "queue", "--replace", environ=GOOD_ENV)
        self.assertEqual(code, 0, err)
        self.assertEqual(raw(home)["session_routing"]["channels"][CH]["busy_policy"], "queue")
        self.assertEqual(len(backups(home)), 2)

    def test_main_target_overrides_busy_policy_only(self):
        home = make_home()
        code, out, err = run(home, "add", "--channel", CH, "--target", "main", "--busy-policy", "queue")
        self.assertEqual(code, 0, err)
        self.assertEqual(raw(home)["session_routing"]["channels"][CH], {"target": "main", "busy_policy": "queue"})
        self.assertIn("no restart needed", out)
        self.assertEqual(run(home, "add", "--channel", "C0OTHER12", "--target", "main",
                             "--url-env", URL_ENV)[0], 2)

    def test_dry_run_writes_nothing(self):
        home = make_home()
        code, out, _ = run(home, *ADD, "--dry-run", environ=GOOD_ENV)
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(raw(home), {"owner_user_id": "U0OWNER"})
        self.assertEqual(backups(home), [])

    def test_keeps_other_routes(self):
        other = {"target": "main", "busy_policy": "queue"}
        home = make_home({"session_routing": {"default": "main", "channels": {"C0OTHER12": other}}})
        self.assertEqual(run(home, *ADD, environ=GOOD_ENV)[0], 0)
        self.assertEqual(raw(home)["session_routing"]["channels"]["C0OTHER12"], other)


class RouteRemoveTests(unittest.TestCase):
    def test_remove_restores_main_and_backs_up(self):
        home = make_home()
        run(home, *ADD, environ=GOOD_ENV)
        code, out, err = run(home, "remove", "--channel", CH)
        self.assertEqual(code, 0, err)
        self.assertEqual(raw(home)["session_routing"]["channels"], {})
        self.assertEqual(len(backups(home)), 2)
        self.assertIn("no restart", out)
        r = common.route_for(common.load_config(home), CH, GOOD_ENV)
        self.assertEqual((r["target"], r["source"]), ("main", "default"))

    def test_remove_missing_and_dry_run(self):
        home = make_home()
        self.assertEqual(run(home, "remove", "--channel", CH)[0], 0)
        self.assertEqual(backups(home), [])
        run(home, *ADD, environ=GOOD_ENV)
        code, out, _ = run(home, "remove", "--channel", CH, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(CH, raw(home)["session_routing"]["channels"])


class ShellWrapperTests(unittest.TestCase):
    """The .sh wrapper against a throwaway install (never the live one)."""

    def setUp(self):
        self.home = make_home()
        (self.home / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.home / ".venv" / "bin" / "python")

    def sh(self, *argv, environ=None):
        env = {k: v for k, v in os.environ.items() if k not in GOOD_ENV}
        env.update(environ or {}, SLACK_BRIDGE_HOME=str(self.home))
        return subprocess.run(["bash", str(SCRIPT), *argv], capture_output=True, text=True, env=env,
                              timeout=60)

    def test_add_without_restart_flag_does_not_restart(self):
        p = self.sh(*ADD[1:], environ=GOOD_ENV)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("restarting", p.stdout)
        self.assertNotIn(URL_VAL, p.stdout + p.stderr)
        self.assertNotIn(AUTH_VAL, p.stdout + p.stderr)
        self.assertIn(CH, raw(self.home)["session_routing"]["channels"])
        self.assertFalse((self.home / "run").exists(), "no bridge start/stop attempted")

    def test_secret_refused_and_remove(self):
        argv = list(ADD[1:])
        argv[argv.index("--url-env") + 1] = URL_VAL
        p = self.sh(*argv, environ=GOOD_ENV)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn(URL_VAL, p.stdout + p.stderr)
        self.sh(*ADD[1:], environ=GOOD_ENV)
        p = self.sh("--remove", "--channel", CH)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(raw(self.home)["session_routing"]["channels"], {})

    def test_dry_run_with_restart_does_not_restart(self):
        p = self.sh(*ADD[1:], "--dry-run", "--restart", environ=GOOD_ENV)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("not restarting", p.stdout)
        self.assertEqual(raw(self.home), {"owner_user_id": "U0OWNER"})

    def test_help(self):
        p = self.sh("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("--url-env", p.stdout)


if __name__ == "__main__":
    unittest.main()
