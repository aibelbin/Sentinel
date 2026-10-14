"""Offline report queue.

A report that cannot be delivered is kept so the server can reconstruct history
from the agent's own timestamps once connectivity returns. Delivery is
at-least-once: a spooled row is deleted only after the server accepts it, so a
crash mid-flush retries rather than loses the report.
"""

import json
import logging
import time

from sentinel_agent import state
from sentinel_agent.config import Config
from sentinel_agent.http import HttpError, TransportError, post

log = logging.getLogger("sentinel.spool")


def enqueue(path: str, payload: dict, max_bytes: int) -> None:
    body = json.dumps(payload)
    with state.connect() as conn:
        conn.execute(
            "INSERT INTO spool (created_ms, path, body) VALUES (?, ?, ?)",
            (int(time.time() * 1000), path, body),
        )
        _evict_to_cap(conn, max_bytes)
    log.info("spooled %s (%d bytes)", path, len(body))


def _evict_to_cap(conn, max_bytes: int) -> None:
    """Drop the oldest reports until the spool fits under the cap.

    Oldest-first, because recent inventory is more useful than stale inventory
    when something has to go.
    """
    total = conn.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM spool").fetchone()[0]
    if total <= max_bytes:
        return

    dropped = 0
    for row_id, length in conn.execute(
        "SELECT id, LENGTH(body) FROM spool ORDER BY created_ms, id"
    ).fetchall():
        if total <= max_bytes:
            break
        conn.execute("DELETE FROM spool WHERE id = ?", (row_id,))
        total -= length
        dropped += 1
    if dropped:
        log.warning("spool over %d bytes: dropped %d oldest report(s)", max_bytes, dropped)


def depth() -> int:
    with state.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]


def flush(config: Config, limit: int = 20) -> int:
    """Deliver spooled reports oldest-first.

    Stops at the first transport failure so a down server does not cause a long
    retry storm every minute. A fatal rejection (revoked key, malformed body)
    discards the row instead, since retrying it forever would block the queue.
    """
    delivered = 0
    discarded = 0
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT id, path, body FROM spool ORDER BY created_ms, id LIMIT ?", (limit,)
        ).fetchall()

    for row_id, path, body in rows:
        try:
            post(config, path, json.loads(body))
        except TransportError as exc:
            log.info("spool flush paused, server unreachable: %s", exc)
            break
        except HttpError as exc:
            if not exc.fatal:
                log.info("spool flush paused: %s", exc)
                break
            log.error("discarding spooled %s, server refused it permanently: %s", path, exc)
            discarded += 1
        except json.JSONDecodeError:
            log.error("discarding unparseable spooled row %d", row_id)
            discarded += 1
        else:
            delivered += 1

        with state.connect() as conn:
            conn.execute("DELETE FROM spool WHERE id = ?", (row_id,))

    if delivered or discarded:
        log.info(
            "spool: %d report(s) delivered, %d discarded as permanently rejected",
            delivered,
            discarded,
        )
    return delivered + discarded


def send_or_spool(config: Config, path: str, payload: dict, max_bytes: int) -> dict | None:
    """Try to deliver now; spool and return None if the server is unreachable."""
    try:
        return post(config, path, payload)
    except TransportError as exc:
        log.warning("%s unreachable (%s), spooling", path, exc)
        enqueue(path, payload, max_bytes)
        return None
    except HttpError as exc:
        if exc.fatal:
            log.error("%s rejected permanently, not spooling: %s", path, exc)
            raise
        log.warning("%s failed (%s), spooling", path, exc)
        enqueue(path, payload, max_bytes)
        return None
