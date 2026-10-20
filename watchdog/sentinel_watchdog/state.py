"""Local persistence: scalar state, cached server config, and the report spool.

SQLite from the standard library, so there is nothing to install. One file,
created on first use.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sentinel_watchdog import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spool (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms INTEGER NOT NULL,
    path       TEXT    NOT NULL,
    body       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_spool_created ON spool (created_ms, id);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    existed = paths.STATE_DB.exists()
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.STATE_DB, timeout=30)
    if not existed:
        # Created by `sudo sentinel-watchdog enroll`, so it would belong to root
        # and the cron job would fail with "attempt to write a readonly
        # database" on every beat. Handed over as soon as it exists.
        from sentinel_watchdog.config import hand_path_to_watchdog_user

        hand_path_to_watchdog_user(paths.STATE_DIR)
        hand_path_to_watchdog_user(paths.STATE_DB)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return default


def set(key: str, value: Any) -> None:  # noqa: A001 - mirrors dict naming deliberately
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
