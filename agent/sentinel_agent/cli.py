"""Agent entrypoint.

Invoked by cron once a minute as `beat`, and by hand for everything else.
Nothing stays resident: each invocation does one pass and exits.
"""

import argparse
import fcntl
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from sentinel_agent import __version__, collectors, config, paths, resources, spool, state, tasks
from sentinel_agent.collectors import host
from sentinel_agent.http import HttpError, InsecureUrlRefused, TransportError, post
from sentinel_agent.log import setup

log = logging.getLogger("sentinel.cli")

LAST_COLLECT_KEY = "last_collect_at"
CACHED_CONFIG_KEY = "cached_config"
LAST_CONTACT_KEY = "last_contact_at"

DEFAULT_CONFIG = {
    "poll_interval_seconds": 60,
    "inventory_interval_seconds": 43200,
    "spool_max_bytes": 67108864,
}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOCKED = 0  # Another run holds the lock: not an error, nothing to do.


class Lock:
    """Non-blocking flock, so a slow collection cannot pile up invocations.

    Cron fires every minute regardless of whether the previous run finished; if
    one is still working, this run exits immediately rather than running a
    second collection concurrently.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "w")  # noqa: SIM115 - released in __exit__
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            self._handle = None
            return False
        return True

    def __exit__(self, *_exc) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cached_config() -> dict:
    cached = state.get(CACHED_CONFIG_KEY) or {}
    merged = dict(DEFAULT_CONFIG)
    if isinstance(cached, dict):
        for key, value in cached.items():
            if key in merged and isinstance(value, int) and value > 0:
                merged[key] = value
    return merged


def _store_config(payload: dict | None) -> dict:
    if isinstance(payload, dict) and payload:
        state.set(CACHED_CONFIG_KEY, payload)
    return _cached_config()


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


def collect_and_submit(cfg: config.Config, agent_config: dict, reason: str) -> dict:
    """Collect inventory and deliver it, spooling if the server is unreachable."""
    log.info("collecting inventory (%s)", reason)
    packages, status = collectors.collect_all()

    payload = {
        "collected_at": _now_iso(),
        "agent_version": __version__,
        "collector_status": status,
        "packages": packages,
        "host": host.facts(),
    }

    response = spool.send_or_spool(
        cfg, "/api/v1/agent/inventory", payload, agent_config["spool_max_bytes"]
    )

    # The collection timestamp advances even when delivery was spooled:
    # re-collecting every minute because the server is down would be wasteful,
    # and the spooled report already carries the correct collected_at.
    state.set(LAST_COLLECT_KEY, payload["collected_at"])

    outcome = {
        "package_count": len(packages),
        "collector_status": status,
        "spooled": response is None,
    }
    if response is not None:
        outcome["change_count"] = response.get("change_count")
        outcome["report_id"] = response.get("report_id")
        if response.get("superseded"):
            log.info("server marked report %s superseded", response.get("report_id"))
        log.info(
            "inventory delivered: %d packages, %s change(s)",
            len(packages),
            response.get("change_count"),
        )
    return outcome


def _inventory_due(agent_config: dict) -> bool:
    last = state.get(LAST_COLLECT_KEY)
    if not last:
        return True
    try:
        previous = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)

    elapsed = (datetime.now(timezone.utc) - previous).total_seconds()
    # A clock that jumped backwards would otherwise postpone collection
    # indefinitely, so treat a negative interval as due.
    return elapsed < 0 or elapsed >= agent_config["inventory_interval_seconds"]


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


def _run_tasks(cfg: config.Config, agent_config: dict, queued: list) -> None:
    context = {
        "collect_now": lambda reason="task": collect_and_submit(cfg, agent_config, reason),
    }

    for task in queued:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id")
        task_type = task.get("type")
        if not task_id or not task_type:
            continue

        log.info("running task %s (%s)", task_id, task_type)
        try:
            result = tasks.dispatch(task_type, task.get("params") or {}, context)
            outcome = {"status": "completed", "result": result, "error": None}
        except tasks.UnsupportedTask as exc:
            # Reported rather than silently dropped, so the dashboard shows the
            # truth about what this agent can and cannot do.
            log.error("task %s rejected: %s", task_id, exc)
            outcome = {"status": "failed", "result": None, "error": str(exc)[:2000]}
        except Exception as exc:  # noqa: BLE001 - every failure must be reported back
            log.exception("task %s failed", task_id)
            outcome = {
                "status": "failed",
                "result": None,
                "error": f"{type(exc).__name__}: {exc}"[:2000],
            }

        try:
            post(cfg, f"/api/v1/agent/tasks/{task_id}/result", outcome)
        except (TransportError, HttpError) as exc:
            # Not spooled: the server marks a task abandoned after 30 minutes,
            # and re-reporting a result for a task the server has given up on
            # would be noise.
            log.warning("could not report result for task %s: %s", task_id, exc)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_enroll(args: argparse.Namespace) -> int:
    cfg = config.Config(
        server_url=args.server.rstrip("/"),
        api_key=args.key,
        insecure=bool(args.insecure),
    )

    payload = {"agent_version": __version__, "host": host.facts()}
    try:
        response = post(cfg, "/api/v1/agent/enroll", payload)
    except InsecureUrlRefused as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except HttpError as exc:
        log.error("enrollment refused: %s", exc)
        return EXIT_ERROR
    except TransportError as exc:
        log.error("cannot reach the server: %s", exc)
        return EXIT_ERROR

    cfg.device_id = response.get("device_id")
    config.save(cfg)
    state.set(CACHED_CONFIG_KEY, response.get("config") or {})

    log.info(
        "enrolled as '%s' (device %s)", response.get("name", "?"), cfg.device_id
    )
    print(cfg.device_id or "")
    return EXIT_OK


def cmd_beat(_args: argparse.Namespace) -> int:
    try:
        cfg = config.load()
    except config.NotEnrolled as exc:
        log.error("%s", exc)
        return EXIT_ERROR

    with Lock(paths.LOCK_PATH) as acquired:
        if not acquired:
            log.debug("another run holds the lock, exiting")
            return EXIT_LOCKED

        agent_config = _cached_config()
        payload = {
            "agent_version": __version__,
            "spool_depth": spool.depth(),
            "resources": resources.sample(),
            "host": host.facts(),
        }

        try:
            response = post(cfg, "/api/v1/agent/heartbeat", payload)
        except InsecureUrlRefused as exc:
            log.error("%s", exc)
            return EXIT_ERROR
        except HttpError as exc:
            log.error("heartbeat refused: %s", exc)
            return EXIT_ERROR
        except TransportError as exc:
            # Offline: still collect if due, so the report is spooled with the
            # right timestamp rather than skipped entirely.
            log.warning("heartbeat failed: %s", exc)
            if _inventory_due(agent_config):
                try:
                    collect_and_submit(cfg, agent_config, reason="scheduled, offline")
                except (HttpError, InsecureUrlRefused) as exc2:
                    log.error("inventory rejected: %s", exc2)
            return EXIT_OK

        state.set(LAST_CONTACT_KEY, _now_iso())
        agent_config = _store_config(response.get("config"))

        # Flush BEFORE collecting. A fresh report carries a newer collected_at,
        # and the server discards any spooled report older than one already
        # applied -- so collecting first would silently destroy the offline
        # history the spool exists to preserve.
        spool.flush(cfg)

        if _inventory_due(agent_config):
            # A permanent rejection here must not abort the beat: the queued
            # tasks below still deserve to run and report back.
            try:
                collect_and_submit(cfg, agent_config, reason="scheduled")
            except (HttpError, InsecureUrlRefused) as exc:
                log.error("inventory rejected: %s", exc)

        queued = response.get("tasks") or []
        if queued:
            _run_tasks(cfg, agent_config, queued)

        return EXIT_OK


def cmd_collect(_args: argparse.Namespace) -> int:
    try:
        cfg = config.load()
    except config.NotEnrolled as exc:
        log.error("%s", exc)
        return EXIT_ERROR

    with Lock(paths.LOCK_PATH) as acquired:
        if not acquired:
            log.error("another run holds the lock; try again shortly")
            return EXIT_ERROR
        try:
            outcome = collect_and_submit(cfg, _cached_config(), reason="manual")
        except (InsecureUrlRefused, HttpError) as exc:
            log.error("%s", exc)
            return EXIT_ERROR

    print(json.dumps(outcome, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_status(_args: argparse.Namespace) -> int:
    try:
        cfg = config.load()
    except config.NotEnrolled as exc:
        print(f"not enrolled: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(
        json.dumps(
            {
                "agent_version": __version__,
                "server_url": cfg.base_url,
                "device_id": cfg.device_id,
                "insecure": cfg.insecure,
                "last_contact_at": state.get(LAST_CONTACT_KEY),
                "last_collect_at": state.get(LAST_COLLECT_KEY),
                "spool_depth": spool.depth(),
                "config": _cached_config(),
                "supported_tasks": tasks.registered_types(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-agent", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="log to file only")
    sub = parser.add_subparsers(dest="command", required=True)

    enroll = sub.add_parser("enroll", help="register this device with the platform")
    enroll.add_argument("--server", required=True, help="base URL, e.g. https://sentinel.example")
    enroll.add_argument("--key", required=True, help="device API key from the dashboard")
    enroll.add_argument(
        "--insecure",
        action="store_true",
        help="permit a plain-HTTP server URL (local testing only)",
    )
    enroll.set_defaults(func=cmd_enroll)

    sub.add_parser("beat", help="one full pass: heartbeat, tasks, due inventory").set_defaults(
        func=cmd_beat
    )
    sub.add_parser("collect", help="collect and submit inventory now").set_defaults(
        func=cmd_collect
    )
    sub.add_parser("status", help="print enrollment and queue state").set_defaults(
        func=cmd_status
    )

    args = parser.parse_args(argv)
    setup(verbose=args.verbose, quiet=args.quiet)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
