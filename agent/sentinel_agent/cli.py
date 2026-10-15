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

from sentinel_agent import (
    __version__,
    collectors,
    config,
    paths,
    resources,
    spool,
    state,
    tasks,
    updater,
)
from sentinel_agent.collectors import host
from sentinel_agent.http import HttpError, InsecureUrlRefused, TransportError, post
from sentinel_agent.log import setup
from sentinel_agent.version import installed_version, running_version

log = logging.getLogger("sentinel.cli")

LAST_COLLECT_KEY = "last_collect_at"
CACHED_CONFIG_KEY = "cached_config"
LAST_CONTACT_KEY = "last_contact_at"
LAST_UPDATE_KEY = "last_update"

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
# Update reporting
# --------------------------------------------------------------------------- #


def _record_update_result(result: str, detail: str, version: str | None = None) -> None:
    """Remember how the last update went, to report on the next heartbeat.

    `version` is the version the result is *about*, which for a revert is the
    one that failed rather than the fallback now running.
    """
    state.set(
        LAST_UPDATE_KEY,
        {"result": result, "detail": detail[:500], "at": _now_iso(),
         "version": version or running_version()},
    )


def _last_update_report() -> dict | None:
    """What the server is told about this machine's last update.

    A revert is only visible from here: the server handed out a version, the
    machine tried it and went back, and nothing else would tell it so.
    """
    record = state.get(LAST_UPDATE_KEY)
    if not isinstance(record, dict):
        return None
    return {
        "result": str(record.get("result", ""))[:32],
        "detail": str(record.get("detail", ""))[:500],
        "at": record.get("at"),
        "version": str(record.get("version", ""))[:64],
    }


def _note_launcher_revert() -> None:
    """Detect that the stable launcher rolled this machine back.

    The launcher cannot report anything itself — it has no credentials and no
    HTTP code — so the first beat after a revert is where that gets noticed.
    """
    pending = state.get("seen_pending_version")
    installed = installed_version()
    if pending and installed and pending != installed:
        log.warning("version %s was reverted; now running %s", pending, installed)
        _record_update_result(
            "reverted",
            f"{pending} failed to run; fell back to {installed}",
            version=pending,
        )
        state.set("seen_pending_version", None)


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


def collect_and_submit(cfg: config.Config, agent_config: dict, reason: str) -> dict:
    """Collect inventory and deliver it, spooling if the server is unreachable."""
    log.info("collecting inventory (%s)", reason)
    packages, status = collectors.collect_all()

    payload = {
        "collected_at": _now_iso(),
        "agent_version": running_version(),
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


class UpdateAttemptFailed(Exception):
    """Carries whether the failure says anything about the build itself.

    Only a build failure should make the server withhold a version from the
    rest of the fleet. A timeout on one machine's network says nothing about
    the release, and quarantining on it would take a healthy version away from
    everyone.
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def _install_update(cfg: config.Config, *, version: str, sha256: str) -> dict:
    """Install a new agent version, recording the outcome either way."""
    try:
        outcome = updater.install(cfg, version, sha256)
    except updater.UpdateFailed as exc:
        # The bundle was wrong, corrupt, or could not run: evidence about the
        # build, so the server should stop handing it out.
        _record_update_result("failed", str(exc), version=version)
        raise UpdateAttemptFailed(str(exc), "build") from exc
    except (TransportError, HttpError) as exc:
        # Could not fetch it. Transient and local to this machine.
        _record_update_result("failed", f"could not fetch {version}: {exc}", version=version)
        raise UpdateAttemptFailed(f"could not fetch the bundle: {exc}", "transient") from exc

    if outcome.get("installed"):
        # Remembered so that if the launcher has to revert, the next beat can
        # work out what happened and say so.
        state.set("seen_pending_version", version)
        _record_update_result(
            "installed", f"{outcome['previous_version']} -> {version}", version=version
        )
    return outcome


def _run_tasks(cfg: config.Config, agent_config: dict, queued: list) -> None:
    context = {
        "collect_now": lambda reason="task": collect_and_submit(cfg, agent_config, reason),
        "install_update": lambda **kw: _install_update(cfg, **kw),
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

    payload = {"agent_version": running_version(), "host": host.facts()}
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

        _note_launcher_revert()

        agent_config = _cached_config()
        payload = {
            "agent_version": running_version(),
            "spool_depth": spool.depth(),
            "resources": resources.sample(),
            "host": host.facts(),
            "update": _last_update_report(),
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

        # Reaching here means a full beat succeeded on this version, which is
        # what takes it off probation. Done last, so a version that breaks any
        # earlier step is never treated as proven.
        if updater.confirm_running_version():
            log.info("version %s completed a beat; it is now the fallback", running_version())
            _record_update_result("ok", f"confirmed {running_version()}")

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


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Prove this build can run, before it is ever swapped in.

    Run out-of-process against a freshly unpacked version by the updater. It
    deliberately touches no network and writes nothing: it answers only "does
    this code load and wire together", which is what distinguishes a broken
    release from a broken server.
    """
    checks: list[tuple[str, str]] = []
    try:
        from sentinel_agent import resources as _resources
        from sentinel_agent import spool as _spool  # noqa: F401
        from sentinel_agent.collectors import host as _host
        from sentinel_agent.collectors import java_pkgs, npm_pkgs, os_pkgs, python_pkgs

        checks.append(("imports", "ok"))

        for name, parser, sample in (
            ("os parser", os_pkgs.parse, "ii\tnginx\t1.24.0\n"),
            ("npm parser", npm_pkgs.parse, '{"dependencies": {}}'),
        ):
            parser(sample)
            checks.append((name, "ok"))

        python_pkgs.parse("[]", scope="selftest")
        checks.append(("python parser", "ok"))
        java_pkgs.parse_jvm_dir([])
        checks.append(("java parser", "ok"))

        if not isinstance(_host.facts(), dict):
            raise RuntimeError("host facts did not return a mapping")
        checks.append(("host facts", "ok"))

        if not isinstance(_resources.sample(), dict):
            raise RuntimeError("resource sample did not return a mapping")
        checks.append(("resource sample", "ok"))

        if tasks.registered_types() != ["collect_inventory", "update_agent"]:
            raise RuntimeError(f"unexpected job types: {tasks.registered_types()}")
        checks.append(("job handlers", "ok"))
    except Exception as exc:  # noqa: BLE001 - any failure means do not install
        for name, result in checks:
            print(f"  {name}: {result}")
        print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    for name, result in checks:
        print(f"  {name}: {result}")
    print(f"  version: {running_version()}")
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
                "agent_version": running_version(),
                "release": __version__,
                "installed_version": installed_version(),
                "confirmed_version": updater.confirmed_version(),
                "versions_on_disk": sorted(
                    entry.name
                    for entry in paths.VERSIONS_DIR.iterdir()
                    if entry.is_dir()
                ) if paths.VERSIONS_DIR.is_dir() else [],
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
    sub.add_parser(
        "selftest", help="check this build can run (used before an update is applied)"
    ).set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    setup(verbose=args.verbose, quiet=args.quiet)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
