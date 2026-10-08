#!/usr/bin/env python3
"""Slack Socket Mode -> agent webhook bridge.

Receives `app_mention` and `message.im` events over Socket Mode, acks them
immediately, filters bot/self/edit/join noise, dedupes retries, optionally
adds an acknowledgement reaction, and POSTs a compact JSON payload to the
agent webhook (GROK_WEBHOOK_URL with Authorization: GROK_WEBHOOK_AUTH).

Secrets come only from environment variables and are never logged.
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import common  # noqa: E402

log = logging.getLogger("slack-bridge")


class Bridge:
    def __init__(self, home: Path, dry_run: bool = False):
        self.home = home
        self.dry_run = dry_run
        self.cfg = common.load_config(home)
        self.dedupe = common.Deduper()
        self.dedupe_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evt")
        self.user_cache: dict[str, dict] = {}
        self.stats = {"received": 0, "forwarded": 0, "skipped": 0, "failed": 0}
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.webhook_url = common.env_value(common.ENV_WEBHOOK_URL)
        self.webhook_auth = common.normalize_auth_header(common.env_value(common.ENV_WEBHOOK_AUTH))
        self.web = None
        self.socket = None
        self.bot_user_id = ""
        self.bot_id = ""

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient

        self.web = WebClient(token=common.env_value(common.ENV_BOT_TOKEN))
        auth = self.web.auth_test()
        self.bot_user_id = auth.get("user_id", "")
        self.bot_id = auth.get("bot_id", "")
        self._remember_identity(auth)
        log.info("authenticated as @%s (%s) in %s (%s)", auth.get("user"),
                 self.bot_user_id, auth.get("team"), auth.get("team_id"))

        self.socket = SocketModeClient(
            app_token=common.env_value(common.ENV_APP_TOKEN),
            web_client=self.web,
            auto_reconnect_enabled=True,
        )
        self.socket.socket_mode_request_listeners.append(self.on_request)
        self.socket.connect()
        log.info("socket mode connected; webhook host=%s; access=%s; owner=%s",
                 common.webhook_host(self.webhook_url), self.cfg.get("access"),
                 self.cfg.get("owner_user_id") or "(unset)")

    def _remember_identity(self, auth) -> None:
        """Record non-secret identity facts in config.json for status/doctor."""
        changed = False
        updates = {
            "bot_user_id": auth.get("user_id", ""),
            "team_id": auth.get("team_id", ""),
            "workspace": auth.get("team", ""),
            "workspace_url": auth.get("url", ""),
        }
        for key, value in updates.items():
            if value and self.cfg.get(key) != value:
                self.cfg[key] = value
                changed = True
        if changed:
            try:
                on_disk = common.load_config(self.home)
                on_disk.update(updates)
                common.save_config(self.home, on_disk)
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("could not update config.json: %s", exc)

    def write_heartbeat(self) -> None:
        state = {
            "pid": os.getpid(),
            "started_at": int(self.started_at),
            "heartbeat_at": int(time.time()),
            "connected": bool(self.socket and self.socket.is_connected()),
            "bot_user_id": self.bot_user_id,
            "webhook_host": common.webhook_host(self.webhook_url),
            **self.stats,
        }
        run = self.home / "run"
        run.mkdir(parents=True, exist_ok=True)
        tmp = run / ".heartbeat.json.tmp"
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, run / "heartbeat.json")

    def serve_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.write_heartbeat()
            except Exception as exc:  # pragma: no cover
                log.warning("heartbeat failed: %s", exc)
            self.stop_event.wait(30)

    def shutdown(self, *_args) -> None:
        log.info("shutting down")
        self.stop_event.set()
        try:
            if self.socket:
                self.socket.close()
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)

    # -- events --------------------------------------------------------------
    def on_request(self, client, req) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        # Ack first, always, so Slack does not retry.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            log.debug("ignoring socket request type %s", req.type)
            return
        self.pool.submit(self.handle_envelope, req.payload, getattr(req, "retry_attempt", None))

    def handle_envelope(self, envelope: dict, retry_attempt=None) -> None:
        try:
            self._handle_envelope(envelope, retry_attempt)
        except Exception:
            self.stats["failed"] += 1
            log.exception("unhandled error processing event %s", envelope.get("event_id"))

    def _handle_envelope(self, envelope: dict, retry_attempt=None) -> None:
        event = envelope.get("event") or {}
        self.stats["received"] += 1
        ok, reason = common.should_handle(event, self.bot_user_id, self.bot_id)
        if not ok:
            self.stats["skipped"] += 1
            log.debug("skip %s: %s", envelope.get("event_id"), reason)
            return
        with self.dedupe_lock:
            dup = self.dedupe.seen(envelope.get("event_id", ""),
                                   f"{event.get('channel')}:{event.get('ts')}")
        if dup:
            self.stats["skipped"] += 1
            log.info("skip duplicate %s (retry=%s)", envelope.get("event_id"), retry_attempt)
            return

        self.cfg = common.load_config(self.home)  # pick up config edits live
        if self.bot_user_id:
            self.cfg["bot_user_id"] = self.bot_user_id
        user = event.get("user", "")
        owner = self.cfg.get("owner_user_id") or ""
        log.info("event %s %s channel=%s user=%s ts=%s len=%d%s",
                 envelope.get("event_id"), event.get("type"), event.get("channel"),
                 user, event.get("ts"), len(event.get("text") or ""),
                 f" text={event.get('text')!r}" if self.cfg.get("log_message_text") else "")

        if self.cfg.get("access") == "owner_only" and owner and user != owner:
            self.stats["skipped"] += 1
            log.info("denied non-owner %s (access=owner_only)", user)
            msg = self.cfg.get("deny_message")
            if msg and not self.dry_run:
                target = common.reply_target(event, bool(self.cfg.get("dm_reply_in_thread")))
                self._safe(self.web.chat_postMessage, channel=target["channel"],
                           thread_ts=target["thread_ts"], text=msg)
            return

        reaction = self.cfg.get("ack_reaction") if self.cfg.get("react_on_receipt") else ""
        if reaction and not self.dry_run:
            self._safe(self.web.reactions_add, channel=event.get("channel"),
                       timestamp=event.get("ts"), name=reaction)

        payload = common.build_payload(envelope, self.cfg, self.home, self.user_info(user))
        if self.dry_run:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        if self.post_webhook(payload):
            self.stats["forwarded"] += 1
        else:
            self.stats["failed"] += 1
            err = self.cfg.get("error_reaction")
            if err:
                self._safe(self.web.reactions_add, channel=event.get("channel"),
                           timestamp=event.get("ts"), name=err)

    def user_info(self, user: str) -> dict:
        if not user or self.web is None:
            return {}
        if user in self.user_cache:
            return self.user_cache[user]
        info = {}
        try:
            resp = self.web.users_info(user=user)
            u = resp.get("user") or {}
            profile = u.get("profile") or {}
            info = {"name": profile.get("display_name") or u.get("name", ""),
                    "real_name": profile.get("real_name") or u.get("real_name", "")}
        except Exception as exc:
            log.warning("users.info failed for %s: %s", user, getattr(exc, "response", exc))
        self.user_cache[user] = info
        return info

    def post_webhook(self, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        retries = max(1, int(self.cfg.get("webhook_retries", 3)))
        timeout = float(self.cfg.get("webhook_timeout_seconds", 20))
        delay = 1.0
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(self.webhook_url, data=body, method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            req.add_header("User-Agent", "slack-bridge/1")
            if self.webhook_auth:
                req.add_header("Authorization", self.webhook_auth)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    log.info("forwarded %s -> HTTP %s", payload.get("event_id"), resp.status)
                    return True
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 404, 410):
                    log.error("webhook rejected event %s with HTTP %s; the webhook URL or "
                              "Authorization value is probably stale. Run scripts/doctor.sh "
                              "and scripts/reconfigure.sh.", payload.get("event_id"), exc.code)
                    return False
                log.warning("webhook HTTP %s (attempt %d/%d)", exc.code, attempt, retries)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log.warning("webhook error %s (attempt %d/%d)",
                            getattr(exc, "reason", exc), attempt, retries)
            if attempt < retries:
                time.sleep(delay)
                delay *= 3
        log.error("giving up on event %s after %d attempts", payload.get("event_id"), retries)
        return False

    def _safe(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except Exception as exc:
            err = getattr(getattr(exc, "response", None), "data", None) or exc
            if isinstance(err, dict) and err.get("error") == "already_reacted":
                return None
            log.warning("%s failed: %s", getattr(fn, "__name__", "slack call"),
                        err.get("error") if isinstance(err, dict) else err)
            return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", help="install directory (default: $SLACK_BRIDGE_HOME or scripts/..)")
    parser.add_argument("--dry-run-event", metavar="JSON_FILE",
                        help="build the webhook payload for an Events API envelope and print it; "
                             "no network calls, no secrets needed")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    home = common.resolve_home(args.home)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("slack_sdk", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.dry_run_event:
        bridge = Bridge(home, dry_run=True)
        with open(args.dry_run_event, encoding="utf-8") as fh:
            envelope = json.load(fh)
        bridge._handle_envelope(envelope)
        return 0

    problems = common.check_env()
    if problems:
        print("slack-bridge: cannot start, environment is incomplete:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Provide the missing values as environment variables (never in files). "
              "See README.md -> 'Secrets'.", file=sys.stderr)
        return 2

    if importlib.util.find_spec("slack_sdk") is None:
        print("slack-bridge: slack_sdk is not installed; run scripts/install.sh", file=sys.stderr)
        return 3

    pidfile = home / "run" / "bridge.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def _cleanup_pidfile() -> None:
        try:
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass

    atexit.register(_cleanup_pidfile)

    bridge = Bridge(home)
    signal.signal(signal.SIGTERM, bridge.shutdown)
    signal.signal(signal.SIGINT, bridge.shutdown)
    try:
        bridge.connect()
    except Exception as exc:
        err = getattr(getattr(exc, "response", None), "data", None)
        log.error("startup failed: %s", (err or {}).get("error") if isinstance(err, dict) else exc)
        return 4
    bridge.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
