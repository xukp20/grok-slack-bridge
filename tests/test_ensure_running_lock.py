"""ensure-running.sh must not leak its flock fd into the bridge it launches.

Regression: the bridge inherited fd 9 (run/ensure-running.lock) from
ensure-running.sh, held the lock for its whole life, and every later check
exited 3 ("another check is in progress"). These tests run the real shell
scripts against a throwaway install whose bridge.py is a tiny fake.
"""
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "slack-bridge" / "scripts"

FAKE_BRIDGE = r'''
import json, os, sys, time
home = sys.argv[sys.argv.index("--home") + 1]
run = os.path.join(home, "run")
os.makedirs(run, exist_ok=True)
open(os.path.join(run, "bridge.pid"), "w").write(str(os.getpid()))
print("socket mode connected", flush=True)
while True:
    now = int(time.time())
    json.dump({"heartbeat_at": now, "connected": True, "last_connected_at": now},
              open(os.path.join(run, "heartbeat.json"), "w"))
    time.sleep(1)
'''

FAKE_ENV = {  # shape-valid placeholders, not real credentials
    "SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test",
    "GROK_WEBHOOK_URL": "https://example.invalid/hook", "GROK_WEBHOOK_AUTH": "test",
}


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("flock") and shutil.which("setsid"),
                     "needs Linux /proc, flock and setsid")
class EnsureRunningLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gsb-ensure-"))
        self.code = self.tmp / "code"
        self.code.mkdir()
        for name in ("lib.sh", "start.sh", "stop.sh", "restart.sh", "ensure-running.sh"):
            shutil.copy2(SCRIPTS / name, self.code / name)
        (self.code / "bridge.py").write_text(FAKE_BRIDGE)
        self.home = self.tmp / "home"
        (self.home / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.home / ".venv" / "bin" / "python")
        (self.home / "config.json").write_text("{}")
        self.env = {k: v for k, v in os.environ.items() if k not in FAKE_ENV}
        self.env.update(FAKE_ENV, SLACK_BRIDGE_HOME=str(self.home))

    def tearDown(self):
        subprocess.run(["bash", str(self.code / "stop.sh")], env=self.env, capture_output=True, timeout=30)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, script, *argv, **kw):
        return subprocess.run(["bash", str(self.code / script), *argv], env=self.env,
                              capture_output=True, text=True, timeout=60, **kw)

    def bridge_pid(self):
        return int((self.home / "run" / "bridge.pid").read_text().strip())

    def assert_lock_not_held_by(self, pid):
        lock = str(self.home / "run" / "ensure-running.lock")
        held = [os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")]
        self.assertNotIn(lock, held, "bridge inherited the ensure-running lock fd")
        with open(lock, "w") as fh:  # and the lock is actually free
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_started_bridge_does_not_hold_lock_and_next_check_passes(self):
        p = self.sh("ensure-running.sh")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("action=started", p.stdout)
        self.assert_lock_not_held_by(self.bridge_pid())
        for _ in range(2):
            p = self.sh("ensure-running.sh", "--quiet")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_restarted_bridge_does_not_hold_lock(self):
        self.assertEqual(self.sh("start.sh").returncode, 0)
        hb = self.home / "run" / "heartbeat.json"
        time.sleep(1.5)
        # Freeze the fake so its heartbeat goes stale and ensure-running restarts it.
        old = self.bridge_pid()
        os.kill(old, 19)  # SIGSTOP
        hb.write_text('{"heartbeat_at": 1, "connected": true}')
        p = self.sh("ensure-running.sh", "--stale-seconds", "5")
        os.kill(old, 18) if Path(f"/proc/{old}").exists() else None
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("action=restarted", p.stdout)
        new = self.bridge_pid()
        self.assertNotEqual(new, old)
        self.assert_lock_not_held_by(new)
        self.assertEqual(self.sh("ensure-running.sh", "--quiet").returncode, 0)

    def test_start_sh_drops_any_inherited_fd(self):
        lock = self.tmp / "caller.lock"
        # A caller holding its own flock on fd 7 while running start.sh.
        p = subprocess.run(["bash", "-c", f'exec 7>"{lock}"; flock -n 7; bash "{self.code}/start.sh"'],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        held = [os.readlink(f"/proc/{self.bridge_pid()}/fd/{fd}")
                for fd in os.listdir(f"/proc/{self.bridge_pid()}/fd")]
        self.assertNotIn(str(lock), held)


if __name__ == "__main__":
    unittest.main()
