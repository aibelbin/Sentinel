"""Offline spool behaviour, the lock, and task dispatch."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sentinel_agent import paths, spool, state, tasks
from sentinel_agent.config import Config
from sentinel_agent.http import HttpError, TransportError

CONFIG = Config(server_url="http://api:8000", api_key="sk_dev_test", insecure=True)
CAP = 1024 * 1024


def payload(index: int = 0) -> dict:
    return {"collected_at": f"2026-10-14T0{index}:00:00+00:00", "packages": []}


# --------------------------------------------------------------------------- #
# Spool
# --------------------------------------------------------------------------- #


def test_enqueue_increases_depth():
    assert spool.depth() == 0
    spool.enqueue("/api/v1/agent/inventory", payload(), CAP)
    assert spool.depth() == 1


def test_flush_delivers_oldest_first(monkeypatch):
    for index in range(3):
        spool.enqueue("/api/v1/agent/inventory", payload(index), CAP)

    seen = []
    monkeypatch.setattr(spool, "post", lambda cfg, path, body: seen.append(body) or {})

    assert spool.flush(CONFIG) == 3
    assert [body["collected_at"] for body in seen] == [
        "2026-10-14T00:00:00+00:00",
        "2026-10-14T01:00:00+00:00",
        "2026-10-14T02:00:00+00:00",
    ]
    assert spool.depth() == 0


def test_flush_stops_at_the_first_transport_failure(monkeypatch):
    """A down server must not cause a retry storm, and ordering must survive."""
    for index in range(3):
        spool.enqueue("/api/v1/agent/inventory", payload(index), CAP)

    calls = {"n": 0}

    def flaky(_cfg, _path, _body):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TransportError("connection refused")
        return {}

    monkeypatch.setattr(spool, "post", flaky)

    assert spool.flush(CONFIG) == 1
    assert spool.depth() == 2  # the remaining two are still queued, in order


def test_flush_discards_a_permanently_rejected_report(monkeypatch):
    """A revoked key will refuse this report forever; keeping it would block
    the queue indefinitely."""
    spool.enqueue("/api/v1/agent/inventory", payload(), CAP)
    monkeypatch.setattr(
        spool,
        "post",
        lambda *_: (_ for _ in ()).throw(HttpError(403, "invalid_or_revoked_key", "nope")),
    )

    assert spool.flush(CONFIG) == 1
    assert spool.depth() == 0


def test_flush_keeps_a_report_the_server_failed_to_process(monkeypatch):
    """A 500 is transient, so the report must be retried rather than dropped."""
    spool.enqueue("/api/v1/agent/inventory", payload(), CAP)
    monkeypatch.setattr(
        spool, "post", lambda *_: (_ for _ in ()).throw(HttpError(503, None, "unavailable"))
    )

    assert spool.flush(CONFIG) == 0
    assert spool.depth() == 1


def test_cap_evicts_the_oldest_entries():
    big = {"packages": ["x" * 2000]}
    for _ in range(10):
        spool.enqueue("/api/v1/agent/inventory", big, max_bytes=5000)

    with state.connect() as conn:
        total = conn.execute("SELECT COALESCE(SUM(LENGTH(body)),0) FROM spool").fetchone()[0]
    assert total <= 5000
    assert spool.depth() < 10


def test_send_or_spool_spools_when_unreachable(monkeypatch):
    monkeypatch.setattr(
        spool, "post", lambda *_: (_ for _ in ()).throw(TransportError("unreachable"))
    )
    assert spool.send_or_spool(CONFIG, "/api/v1/agent/inventory", payload(), CAP) is None
    assert spool.depth() == 1


def test_send_or_spool_raises_without_spooling_on_a_fatal_rejection(monkeypatch):
    monkeypatch.setattr(
        spool, "post", lambda *_: (_ for _ in ()).throw(HttpError(403, "device_disabled", "no"))
    )
    with pytest.raises(HttpError):
        spool.send_or_spool(CONFIG, "/api/v1/agent/inventory", payload(), CAP)
    assert spool.depth() == 0


# --------------------------------------------------------------------------- #
# Lock
# --------------------------------------------------------------------------- #


def test_lock_is_exclusive():
    from sentinel_agent.cli import Lock

    with Lock(paths.LOCK_PATH) as first:
        assert first is True
        with Lock(paths.LOCK_PATH) as second:
            assert second is False


def test_lock_is_released_on_exit():
    from sentinel_agent.cli import Lock

    with Lock(paths.LOCK_PATH) as acquired:
        assert acquired is True
    with Lock(paths.LOCK_PATH) as acquired:
        assert acquired is True


def test_beat_exits_zero_while_another_run_holds_the_lock(sandbox, monkeypatch):
    """Cron fires every minute regardless; an overlapping run must be a quiet
    no-op, not an error mail to root."""
    from sentinel_agent import config as config_module
    from sentinel_agent.cli import Lock, cmd_beat

    config_module.save(CONFIG)

    with Lock(paths.LOCK_PATH):
        # No HTTP stub is installed: reaching the network would mean the lock
        # was not honoured.
        assert cmd_beat(argparse_stub()) == 0


def argparse_stub():
    import argparse

    return argparse.Namespace(verbose=False, quiet=True)


# --------------------------------------------------------------------------- #
# Task dispatch
# --------------------------------------------------------------------------- #


def test_unknown_task_type_is_refused():
    with pytest.raises(tasks.UnsupportedTask, match="unsupported task type"):
        tasks.dispatch("rm -rf /", {}, {})


def test_unexpected_parameters_are_refused():
    """A handler never sees a parameter it did not declare."""
    with pytest.raises(tasks.UnsupportedTask, match="unexpected parameters"):
        tasks.dispatch("collect_inventory", {"command": "whoami"}, {})


def test_non_object_params_are_refused():
    with pytest.raises(tasks.UnsupportedTask):
        tasks.dispatch("collect_inventory", ["ls"], {})


def test_collect_inventory_runs_the_local_handler():
    called = {}

    def fake_collect(reason: str = "task") -> dict:
        called["reason"] = reason
        return {"package_count": 7, "change_count": 2, "spooled": False}

    result = tasks.dispatch("collect_inventory", {}, {"collect_now": fake_collect})

    assert called["reason"] == "task"
    assert result["package_count"] == 7


def test_only_expected_task_types_are_registered():
    assert tasks.registered_types() == ["collect_inventory"]


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def test_status_exits_nonzero_when_not_enrolled(sandbox):
    agent_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(agent_root / "bin" / "sentinel-agent"), "status"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "SENTINEL_CONFIG": str(sandbox / "etc" / "config.json"),
            "SENTINEL_STATE_DIR": str(sandbox / "state"),
            "SENTINEL_LOG_DIR": str(sandbox / "log"),
        },
        check=False,
    )
    assert result.returncode != 0
    assert "not enrolled" in result.stderr


def test_config_is_written_mode_600(sandbox):
    """The API key must never be readable by other users on the device."""
    from sentinel_agent import config as config_module

    config_module.save(CONFIG)
    mode = paths.CONFIG_PATH.stat().st_mode & 0o777
    assert mode == 0o600


def test_config_round_trips():
    from sentinel_agent import config as config_module

    config_module.save(Config(server_url="https://x/", api_key="k", device_id="d"))
    loaded = config_module.load()
    assert loaded.base_url == "https://x"
    assert loaded.device_id == "d"


def test_config_missing_fields_is_not_enrolled():
    from sentinel_agent import config as config_module

    paths.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG_PATH.write_text(json.dumps({"server_url": "https://x"}), encoding="utf-8")
    with pytest.raises(config_module.NotEnrolled, match="api_key"):
        config_module.load()
