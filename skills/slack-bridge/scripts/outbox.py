"""Outgoing Slack text: mention escaping and a bounded, rate-limited outbox.

Everything the bridge itself posts (deny/stop/help/status/error texts,
monitoring reports) goes through Outbox; replies posted by reply.sh go
through sanitize() too.

sanitize():
  - <!channel>, <!here>, <!everyone> and <!subteam^…> never ping: they are
    rendered as inert text ("@\u200bhere").
  - <@U…> user mentions ping only when the ID is in the allowed set (owner,
    the requester, allow-listed bots, mention_allowlist, --allow-mention);
    any other mention is rendered as inert text.
"""

from __future__ import annotations

import collections
import logging
import re
import threading
import time
from typing import Callable, Iterable

log = logging.getLogger("slack-bridge.outbox")

ZWSP = "\u200b"
BROADCAST_RE = re.compile(r"<!(channel|here|everyone)(?:\|[^>]*)?>|<!subteam\^([A-Z0-9]+)(?:\|([^>]*))?>")
USER_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")


def sanitize(text: str, allowed_users: Iterable[str] = ()) -> str:
    allowed = {u for u in allowed_users if u}

    def broadcast(m: re.Match) -> str:
        if m.group(1):
            return f"@{ZWSP}{m.group(1)}"
        return f"@{ZWSP}{m.group(3) or 'group'}"

    def user(m: re.Match) -> str:
        if m.group(1) in allowed:
            return m.group(0)
        return f"@{ZWSP}{m.group(2) or m.group(1)}"

    text = BROADCAST_RE.sub(broadcast, text or "")
    return USER_RE.sub(user, text)


class Outbox:
    """Bounded FIFO of bridge-originated messages, sent at most one per
    `min_interval` seconds. When full, the oldest message is dropped (and
    counted) rather than blocking event handling.
    """

    def __init__(self, send: Callable[[dict], None], *, max_items: int = 50,
                 min_interval: float = 1.0, sync: bool = False, now=time.monotonic):
        self.send = send
        self.items: collections.deque = collections.deque()
        self.max_items = max(1, max_items)
        self.min_interval = max(0.0, min_interval)
        self.sync = sync
        self.now = now
        self.dropped = 0
        self.sent = 0
        self.last_sent = 0.0
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.thread: threading.Thread | None = None
        self.stopping = False

    def put(self, channel: str, thread_ts: str | None, text: str, allowed_users: Iterable[str] = ()) -> None:
        msg = {"channel": channel, "text": sanitize(text, allowed_users)}
        if thread_ts:
            msg["thread_ts"] = thread_ts
        with self.lock:
            if len(self.items) >= self.max_items:
                self.items.popleft()
                self.dropped += 1
                log.warning("outbox full (%d); dropped the oldest message", self.max_items)
            self.items.append(msg)
        if self.sync:
            self.drain(wait=False)
        else:
            self.event.set()

    def drain(self, wait: bool = True) -> int:
        n = 0
        while True:
            with self.lock:
                if not self.items:
                    return n
                gap = self.min_interval - (self.now() - self.last_sent)
                if gap > 0 and wait:
                    pass
                else:
                    msg = self.items.popleft()
                    gap = 0
            if gap > 0:
                time.sleep(gap)
                continue
            try:
                self.send(msg)
                self.sent += 1
            except Exception as exc:  # never let a post failure break event handling
                log.warning("outbox post failed: %s", type(exc).__name__)
            self.last_sent = self.now()
            n += 1

    def start(self) -> None:
        if self.sync or self.thread:
            return

        def loop():
            while not self.stopping:
                self.event.wait(5)
                self.event.clear()
                self.drain()

        self.thread = threading.Thread(target=loop, name="outbox", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping = True
        self.event.set()

    def __len__(self) -> int:
        return len(self.items)
