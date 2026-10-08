"""Persistent receipt queue, operation state machine and thread state (SQLite).

One database file (``<home>/run/bridge.sqlite``) shared by the bridge process
and the helper CLIs (reply.sh / slackctl.py). WAL mode + busy timeout make
concurrent short writes from both sides safe.

Operation (receipt) states::

    received ─┬─> queued ──> submitted ──┬─> accepted ──┬─> completed
              │      │                   │              ├─> no_reply
              │      │                   │              └─> stopped
              │      │                   ├─> unknown-result   (timeout after send)
              │      │                   ├─> queued           (definitely not delivered, retry)
              │      │                   └─> failed           (rejected / retries exhausted)
              │      └─> ignored / stopped                    (stop checked before send)
              ├─> ignored        (filtered, refused, loop control: explicit)
              └─> rejected       (same id or message, different content)

On startup ``submitted``/``accepted`` become ``needs-reconciliation``; they are
never replayed automatically. ``unknown-result`` is never resent blindly.

Thread cursors only advance over messages whose receipts are *handled*
(accepted, completed, no_reply, stopped, ignored, rejected) with no earlier
unhandled message in the same thread. A failed or partial read never moves a
cursor.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

STATES = (
    "received", "queued", "submitted", "accepted", "completed", "no_reply",
    "stopped", "ignored", "rejected", "unknown-result", "needs-reconciliation",
    "failed",
)
HANDLED = ("accepted", "completed", "no_reply", "stopped", "ignored", "rejected")
BLOCKING = ("received", "queued", "submitted", "unknown-result", "needs-reconciliation", "failed")
TERMINAL = ("completed", "no_reply", "stopped", "ignored", "rejected", "failed")

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "received": ("queued", "ignored", "rejected", "stopped"),
    "queued": ("submitted", "ignored", "stopped"),
    "submitted": ("accepted", "unknown-result", "queued", "failed"),
    "accepted": ("completed", "no_reply", "stopped", "needs-reconciliation"),
    # "queued" from these two only via an explicit operator retry (slackctl ops retry --force).
    "unknown-result": ("completed", "no_reply", "stopped", "ignored", "queued"),
    "needs-reconciliation": ("completed", "no_reply", "stopped", "ignored", "queued"),
    "failed": ("queued", "ignored"),
    "completed": (),
    "no_reply": (),
    "stopped": (),
    "ignored": (),
    "rejected": (),
}

# Thread task states. Bot-authored messages are only forwarded while a
# thread is "active" (or its last turn "completed").
THREAD_STATES = ("active", "completed", "no_reply", "stopped", "paused")


class TransitionError(Exception):
    pass


def fingerprint(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def message_fingerprint(channel: str, ts: str, actor: str, text: str) -> str:
    return fingerprint("msg", channel or "", ts or "", actor or "", (text or "").strip())


def thread_key(team_id: str, channel: str, root_ts: str, app_id: str) -> str:
    return f"{team_id or '-'}:{channel or '-'}:{root_ts or '-'}:{app_id or '-'}"


def _ts_key(ts: str | None) -> str:
    """Sortable text form of a Slack ts ("1712345678.000100")."""
    if not ts:
        return ""
    sec, _, frac = str(ts).partition(".")
    return f"{int(sec):012d}.{(frac or '0'):0<6}"


class Store:
    def __init__(self, path: Path | str, now=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- schema --------------------------------------------------------------
    def _migrate(self) -> None:
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS receipts(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              op_id TEXT NOT NULL UNIQUE,
              msg_key TEXT UNIQUE,
              fingerprint TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT '',
              team_id TEXT, channel TEXT, ts TEXT, ts_key TEXT,
              thread_key TEXT, task_id INTEGER,
              actor TEXT, actor_type TEXT,
              state TEXT NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              attempts INTEGER NOT NULL DEFAULT 0,
              not_before REAL NOT NULL DEFAULT 0,
              payload TEXT,
              envelope TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS receipts_state ON receipts(state, not_before, id);
            CREATE INDEX IF NOT EXISTS receipts_thread ON receipts(thread_key, ts_key);
            CREATE TABLE IF NOT EXISTS transitions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT NOT NULL,
              from_state TEXT, to_state TEXT NOT NULL, reason TEXT, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS threads(
              thread_key TEXT PRIMARY KEY,
              team_id TEXT, channel TEXT, root_ts TEXT, app_id TEXT,
              task_id INTEGER NOT NULL DEFAULT 1,
              state TEXT NOT NULL DEFAULT 'active',
              state_reason TEXT NOT NULL DEFAULT '',
              state_at REAL NOT NULL DEFAULT 0,
              following INTEGER NOT NULL DEFAULT 0,
              cursor_ts TEXT NOT NULL DEFAULT '',
              catchup_ok INTEGER NOT NULL DEFAULT 1,
              bot_turns INTEGER NOT NULL DEFAULT 0,
              last_bot_at REAL NOT NULL DEFAULT 0,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS counters(
              key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0, updated_at REAL);
            """)
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                            (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # -- receipts --------------------------------------------------------------
    def get(self, op_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM receipts WHERE op_id=?", (op_id,)).fetchone()
        return dict(row) if row else None

    def record(self, op_id: str, fp: str, *, msg_key: str | None = None, kind: str = "",
               team_id: str = "", channel: str = "", ts: str = "", thread_key: str = "",
               actor: str = "", actor_type: str = "",
               envelope: dict | None = None) -> tuple[str, dict]:
        """Persist a receipt before anything else happens.

        Returns (status, row) where status is "new", "duplicate" (same id or
        same message with the same content) or "conflict" (same id or message
        with a different fingerprint; the new event is rejected).
        """
        now = self.now()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.db.execute(
                    "SELECT * FROM receipts WHERE op_id=? OR (msg_key IS NOT NULL AND msg_key=?)",
                    (op_id, msg_key)).fetchall()
                if existing:
                    row = dict(existing[0])
                    status = "duplicate" if all(r["fingerprint"] == fp for r in existing) else "conflict"
                    if status == "conflict":
                        self._log(op_id, None, "rejected",
                                  f"fingerprint conflict with {row['op_id']}", now)
                    self.db.execute("COMMIT")
                    return status, row
                self.db.execute(
                    "INSERT INTO receipts(op_id, msg_key, fingerprint, kind, team_id, channel, ts, "
                    "ts_key, thread_key, actor, actor_type, state, envelope, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'received',?,?,?)",
                    (op_id, msg_key, fp, kind, team_id, channel, ts, _ts_key(ts), thread_key,
                     actor, actor_type,
                     json.dumps(envelope, ensure_ascii=False) if envelope is not None else None,
                     now, now))
                self._log(op_id, None, "received", kind, now)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return "new", self.get(op_id)

    def _log(self, op_id: str, frm: str | None, to: str, reason: str, at: float) -> None:
        self.db.execute("INSERT INTO transitions(op_id, from_state, to_state, reason, at) "
                        "VALUES(?,?,?,?,?)", (op_id, frm, to, reason, at))

    def transition(self, op_id: str, to: str, reason: str = "", *, expect: Iterable[str] | None = None,
                   payload: dict | None = None, not_before: float | None = None,
                   attempts_inc: int = 0, task_id: int | None = None) -> dict:
        """Move an operation to a new state, enforcing the state machine."""
        if to not in STATES:
            raise TransitionError(f"unknown state {to}")
        now = self.now()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM receipts WHERE op_id=?", (op_id,)).fetchone()
                if row is None:
                    raise TransitionError(f"unknown operation {op_id}")
                frm = row["state"]
                if expect is not None and frm not in tuple(expect):
                    raise TransitionError(f"{op_id} is {frm}, expected {tuple(expect)}")
                if to != frm and to not in TRANSITIONS.get(frm, ()):
                    raise TransitionError(f"{op_id}: {frm} -> {to} not allowed")
                sets = ["state=?", "reason=?", "updated_at=?", "attempts=attempts+?"]
                vals: list[Any] = [to, reason[:500], now, attempts_inc]
                if payload is not None:
                    sets.append("payload=?")
                    vals.append(json.dumps(payload, ensure_ascii=False))
                if not_before is not None:
                    sets.append("not_before=?")
                    vals.append(not_before)
                if task_id is not None:
                    sets.append("task_id=?")
                    vals.append(task_id)
                if to in TERMINAL:
                    sets.append("envelope=NULL")
                self.db.execute(f"UPDATE receipts SET {', '.join(sets)} WHERE op_id=?", (*vals, op_id))
                self._log(op_id, frm, to, reason, now)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        result = self.get(op_id)
        if result and result.get("thread_key"):
            self.advance_cursor(result["thread_key"])
        return result

    def next_queued(self) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM receipts WHERE state='queued' AND not_before<=? ORDER BY id LIMIT 1",
                (self.now(),)).fetchone()
        return dict(row) if row else None

    def recover_after_restart(self) -> list[dict]:
        """submitted/accepted -> needs-reconciliation (never replayed)."""
        now = self.now()
        with self.lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM receipts WHERE state IN ('submitted','accepted')").fetchall()]
            for r in rows:
                self.db.execute("UPDATE receipts SET state='needs-reconciliation', reason=?, "
                                "updated_at=? WHERE op_id=?",
                                (f"bridge restarted while {r['state']}", now, r["op_id"]))
                self._log(r["op_id"], r["state"], "needs-reconciliation", "restart", now)
            # received but never decided: the decision is recomputed by the caller.
        return rows

    def undecided(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM receipts WHERE state='received' ORDER BY id").fetchall()]

    def latest_open_op(self, channel: str, thread_ts: str | None) -> dict | None:
        """Most recent accepted/needs-reconciliation/unknown-result op for a reply target."""
        with self.lock:
            q = ("SELECT r.* FROM receipts r LEFT JOIN threads t ON r.thread_key=t.thread_key "
                 "WHERE r.channel=? AND r.state IN ('accepted','needs-reconciliation','unknown-result') ")
            args: list[Any] = [channel]
            if thread_ts:
                q += "AND t.root_ts=? "
                args.append(thread_ts)
            q += "ORDER BY r.id DESC LIMIT 1"
            row = self.db.execute(q, args).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.db.execute("SELECT state, COUNT(*) n FROM receipts GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def list_ops(self, states: Iterable[str] | None = None, limit: int = 50) -> list[dict]:
        with self.lock:
            if states:
                st = tuple(states)
                q = f"SELECT * FROM receipts WHERE state IN ({','.join('?' * len(st))}) ORDER BY id DESC LIMIT ?"
                rows = self.db.execute(q, (*st, limit)).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def history(self, op_id: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM transitions WHERE op_id=? ORDER BY id", (op_id,)).fetchall()]

    # -- threads -----------------------------------------------------------------
    def thread(self, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM threads WHERE thread_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def ensure_thread(self, key: str, team_id: str, channel: str, root_ts: str, app_id: str) -> dict:
        now = self.now()
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO threads(thread_key, team_id, channel, root_ts, app_id, "
                "state_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (key, team_id, channel, root_ts, app_id, now, now, now))
        return self.thread(key)

    def find_thread(self, channel: str, root_ts: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM threads WHERE channel=? AND root_ts=? "
                                  "ORDER BY updated_at DESC LIMIT 1", (channel, root_ts)).fetchone()
        return dict(row) if row else None

    def update_thread(self, key: str, **fields: Any) -> dict | None:
        if not fields:
            return self.thread(key)
        now = self.now()
        if "state" in fields:
            if fields["state"] not in THREAD_STATES:
                raise ValueError(f"unknown thread state {fields['state']}")
            fields.setdefault("state_at", now)
        fields["updated_at"] = now
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE threads SET {cols} WHERE thread_key=?", (*fields.values(), key))
        return self.thread(key)

    def add_bot_turn(self, key: str) -> dict | None:
        with self.lock:
            self.db.execute("UPDATE threads SET bot_turns=bot_turns+1, last_bot_at=?, updated_at=? "
                            "WHERE thread_key=?", (self.now(), self.now(), key))
        return self.thread(key)

    def new_task(self, key: str, reason: str = "owner started a new task") -> dict | None:
        with self.lock:
            self.db.execute("UPDATE threads SET task_id=task_id+1, bot_turns=0, last_bot_at=0, "
                            "state='active', state_reason=?, state_at=?, updated_at=? WHERE thread_key=?",
                            (reason, self.now(), self.now(), key))
        return self.thread(key)

    def pause_bot_threads(self, reason: str = "bridge restarted") -> int:
        """After a restart, threads with bot activity stay paused until resumed."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE threads SET state='paused', state_reason=?, state_at=?, updated_at=? "
                "WHERE bot_turns>0 AND state IN ('active','completed')",
                (reason, self.now(), self.now()))
            return cur.rowcount

    def following_threads(self, since: float) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM threads WHERE following=1 AND updated_at>=? AND state!='stopped' "
                "ORDER BY updated_at DESC", (since,)).fetchall()]

    def list_threads(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM threads ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()]

    def advance_cursor(self, key: str) -> str:
        """Move the thread cursor over contiguous handled messages; never past a blocker."""
        hs = ",".join("?" * len(HANDLED))
        bs = ",".join("?" * len(BLOCKING))
        with self.lock:
            blocker = self.db.execute(
                f"SELECT MIN(ts_key) b FROM receipts WHERE thread_key=? AND ts_key!='' AND state IN ({bs})",
                (key, *BLOCKING)).fetchone()["b"]
            q = f"SELECT ts, ts_key FROM receipts WHERE thread_key=? AND ts_key!='' AND state IN ({hs})"
            args: list[Any] = [key, *HANDLED]
            if blocker:
                q += " AND ts_key<?"
                args.append(blocker)
            q += " ORDER BY ts_key DESC LIMIT 1"
            row = self.db.execute(q, args).fetchone()
            cur = self.db.execute("SELECT cursor_ts FROM threads WHERE thread_key=?", (key,)).fetchone()
            if not row or cur is None:
                return cur["cursor_ts"] if cur else ""
            if _ts_key(row["ts"]) > _ts_key(cur["cursor_ts"]):
                self.db.execute("UPDATE threads SET cursor_ts=? WHERE thread_key=?", (row["ts"], key))
                return row["ts"]
            return cur["cursor_ts"]

    def prune(self, older_than_days: float = 30) -> int:
        """Drop terminal receipts (and their transitions) older than N days."""
        cutoff = self.now() - older_than_days * 86400
        ts = ",".join("?" * len(TERMINAL))
        with self.lock:
            ids = [r["op_id"] for r in self.db.execute(
                f"SELECT op_id FROM receipts WHERE updated_at<? AND state IN ({ts})",
                (cutoff, *TERMINAL)).fetchall()]
            for op in ids:
                self.db.execute("DELETE FROM transitions WHERE op_id=?", (op,))
                self.db.execute("DELETE FROM receipts WHERE op_id=?", (op,))
        return len(ids)

    # -- counters (monitoring) ---------------------------------------------------
    def bump(self, key: str, by: int = 1) -> int:
        with self.lock:
            self.db.execute("INSERT INTO counters(key, value, updated_at) VALUES(?, ?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value, "
                            "updated_at=excluded.updated_at", (key, by, self.now()))
            return self.counter(key)

    def counter(self, key: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM counters WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row else 0

    def reset_counter(self, key: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM counters WHERE key=?", (key,))

    def meta(self, key: str, value: str | None = None) -> str | None:
        with self.lock:
            if value is not None:
                self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) "
                                "DO UPDATE SET value=excluded.value", (key, value))
                return value
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def open_store(home: Path) -> Store:
    return Store(Path(home) / "run" / "bridge.sqlite")
