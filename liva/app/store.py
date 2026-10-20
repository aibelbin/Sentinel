"""Liva's own SQLite file: chats, messages, pending approvals and the audit log.

Deliberately separate from Sentinel's PostgreSQL, so a corrupt or full file can
only hurt Liva.
"""

import json
import sqlite3
import threading
import time
import uuid

from .security import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS pending (
    call_id TEXT NOT NULL, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    tool TEXT NOT NULL, arguments TEXT NOT NULL, description TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, call_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, owner TEXT NOT NULL, chat_id TEXT,
    tool TEXT NOT NULL, arguments TEXT NOT NULL, tier TEXT NOT NULL, mode TEXT NOT NULL,
    ok INTEGER NOT NULL, detail TEXT NOT NULL, duration_ms INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _run(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            return cursor

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # ---- chats ---------------------------------------------------------- #

    def create_chat(self, owner: str, title: str = "New chat") -> dict:
        now = time.time()
        chat = {"id": uuid.uuid4().hex, "owner": owner, "title": title,
                "created_at": now, "updated_at": now}
        self._run("INSERT INTO chats VALUES (:id, :owner, :title, :created_at, :updated_at)", chat)
        return chat

    def list_chats(self, owner: str) -> list[dict]:
        rows = self._all("SELECT * FROM chats WHERE owner = ? ORDER BY updated_at DESC", (owner,))
        return [dict(row) for row in rows]

    def get_chat(self, chat_id: str, owner: str) -> dict | None:
        rows = self._all("SELECT * FROM chats WHERE id = ? AND owner = ?", (chat_id, owner))
        return dict(rows[0]) if rows else None

    def delete_chat(self, chat_id: str, owner: str) -> None:
        self._run("DELETE FROM chats WHERE id = ? AND owner = ?", (chat_id, owner))

    def set_title(self, chat_id: str, title: str) -> None:
        self._run("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))

    # ---- messages ------------------------------------------------------- #

    def add_message(self, chat_id: str, payload: dict) -> None:
        self._run(
            "INSERT INTO messages (chat_id, role, payload, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, payload["role"], json.dumps(payload), time.time()),
        )
        self._run("UPDATE chats SET updated_at = ? WHERE id = ?", (time.time(), chat_id))

    def display_messages(self, chat_id: str) -> list[dict]:
        rows = self._all("SELECT payload FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [json.loads(row["payload"]) for row in rows]

    def model_messages(self, chat_id: str) -> list[dict]:
        return [m for m in self.display_messages(chat_id) if m["role"] != "ui"]

    # ---- pending approvals --------------------------------------------- #

    def create_pending(self, call_id: str, chat_id: str, tool: str, args: dict, description: str) -> None:
        self._run(
            "INSERT OR IGNORE INTO pending VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (call_id, chat_id, tool, json.dumps(args), description, time.time()),
        )

    @staticmethod
    def _pending(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["arguments"] = json.loads(data["arguments"])
        return data

    def get_pending(self, chat_id: str, call_id: str) -> dict | None:
        rows = self._all("SELECT * FROM pending WHERE chat_id = ? AND call_id = ?", (chat_id, call_id))
        return self._pending(rows[0]) if rows else None

    def set_pending_status(self, chat_id: str, call_id: str, status: str) -> None:
        self._run(
            "UPDATE pending SET status = ? WHERE chat_id = ? AND call_id = ?", (status, chat_id, call_id)
        )

    def delete_pending(self, chat_id: str, call_id: str) -> None:
        self._run("DELETE FROM pending WHERE chat_id = ? AND call_id = ?", (chat_id, call_id))

    def list_pending(self, chat_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM pending WHERE chat_id = ? AND status = 'pending' ORDER BY created_at", (chat_id,)
        )
        return [self._pending(row) for row in rows]

    # ---- audit ---------------------------------------------------------- #

    def add_audit(self, *, owner: str, chat_id: str | None, tool: str, args: dict, tier: str,
                  mode: str, ok: bool, detail: str, duration_ms: int) -> None:
        self._run(
            "INSERT INTO audit (ts, owner, chat_id, tool, arguments, tier, mode, ok, detail, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), owner, chat_id, tool, redact(json.dumps(args, default=str)), tier, mode,
             int(ok), redact(detail), duration_ms),
        )

    def list_audit(self, owner: str, limit: int = 100) -> list[dict]:
        rows = self._all("SELECT * FROM audit WHERE owner = ? ORDER BY id DESC LIMIT ?", (owner, limit))
        return [dict(row) for row in rows]

    # ---- retention ------------------------------------------------------ #

    def prune(self, retention_days: int, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - retention_days * 86400
        removed = self._run("DELETE FROM chats WHERE updated_at < ?", (cutoff,)).rowcount
        removed += self._run("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
        return removed
