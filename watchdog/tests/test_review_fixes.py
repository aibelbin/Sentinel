"""Regression tests for watchdog defects found in review."""

import argparse
import json

import pytest

from sentinel_watchdog import cli, config as config_module, spool, state
from sentinel_watchdog.config import Config
from sentinel_watchdog.http import HttpError, TransportError

CONFIG = Config(server_url="http://api:8000", api_key="sk_dev_test", insecure=True)


def args():
    return argparse.Namespace(verbose=False, quiet=True)


# --------------------------------------------------------------------------- #
# The spool must be flushed before a fresh collection supersedes it
# --------------------------------------------------------------------------- #


def test_beat_flushes_the_spool_before_collecting(sandbox, monkeypatch):
    """The original defect: a fresh report carries a newer collected_at, and the
    server discards any spooled report older than one already applied. Collecting
    first therefore destroyed exactly the offline history the spool exists for."""
    config_module.save(CONFIG)
    spool.enqueue("/api/v1/watchdog/inventory", {"collected_at": "2026-10-01T00:00:00+00:00"}, 10**7)

    order = []

    def fake_post(_cfg, path, _payload):
        if path.endswith("/heartbeat"):
            return {"config": {}, "tasks": []}
        order.append(path)
        return {"change_count": 0, "report_id": 1}

    monkeypatch.setattr(cli, "post", fake_post)
    monkeypatch.setattr(spool, "post", fake_post)
    monkeypatch.setattr(cli.collectors, "collect_all", lambda: ([], {}))
    monkeypatch.setattr(cli.host, "facts", lambda: {})

    assert cli.cmd_beat(args()) == 0

    # Two inventory POSTs: the spooled one, then the fresh collection.
    assert len(order) == 2
    assert spool.depth() == 0


def test_beat_survives_a_fatal_inventory_rejection(sandbox, monkeypatch):
    """A 403 on the inventory POST used to escape as an unhandled traceback,
    so queued tasks were dropped and the spool never flushed."""
    config_module.save(CONFIG)
    reported = []

    def fake_post(_cfg, path, payload):
        if path.endswith("/heartbeat"):
            return {
                "config": {},
                "tasks": [{"id": "11111111-1111-1111-1111-111111111111",
                           "type": "collect_inventory", "params": {}}],
            }
        if path.endswith("/inventory"):
            raise HttpError(403, "invalid_or_revoked_key", "nope")
        reported.append(payload)
        return {}

    monkeypatch.setattr(cli, "post", fake_post)
    monkeypatch.setattr(spool, "post", fake_post)
    monkeypatch.setattr(cli.collectors, "collect_all", lambda: ([], {}))
    monkeypatch.setattr(cli.host, "facts", lambda: {})

    # No traceback, and the queued task still ran and reported back.
    assert cli.cmd_beat(args()) == 0
    assert len(reported) == 1
    assert reported[0]["status"] == "failed"


def test_beat_still_collects_while_the_server_is_unreachable(sandbox, monkeypatch):
    config_module.save(CONFIG)

    def fake_post(_cfg, path, _payload):
        raise TransportError("connection refused")

    monkeypatch.setattr(cli, "post", fake_post)
    monkeypatch.setattr(spool, "post", fake_post)
    monkeypatch.setattr(cli.collectors, "collect_all", lambda: ([], {}))
    monkeypatch.setattr(cli.host, "facts", lambda: {})

    assert cli.cmd_beat(args()) == 0
    # The report was spooled with its own timestamp rather than lost.
    assert spool.depth() == 1


# --------------------------------------------------------------------------- #
# One broken interpreter must not discard the others' packages
# --------------------------------------------------------------------------- #


def test_one_unparseable_interpreter_keeps_the_rest(monkeypatch):
    from sentinel_watchdog.collectors import python_pkgs

    monkeypatch.setattr(
        python_pkgs, "find_interpreters", lambda: ["/usr/bin/python3.10", "/usr/bin/python3.12"]
    )

    def fake_run(argv, timeout=60):
        if "python3.10" in argv[0]:
            return json.dumps([{"name": "requests", "version": "2.31.0"}])
        return "WARNING: something printed a banner instead of JSON"

    monkeypatch.setattr(python_pkgs, "run", fake_run)

    found = python_pkgs.collect()
    assert [p["name"] for p in found] == ["requests"]
    assert found[0]["scope"] == "/usr/bin/python3.10"


# --------------------------------------------------------------------------- #
# Spool accounting
# --------------------------------------------------------------------------- #


def test_flush_reports_delivered_and_discarded_separately(monkeypatch, caplog):
    spool.enqueue("/api/v1/watchdog/inventory", {"a": 1}, 10**7)
    spool.enqueue("/api/v1/watchdog/inventory", {"b": 2}, 10**7)

    calls = {"n": 0}

    def fake_post(_cfg, _path, _body):
        calls["n"] += 1
        if calls["n"] == 2:
            raise HttpError(422, "bad_body", "malformed")
        return {}

    monkeypatch.setattr(spool, "post", fake_post)
    with caplog.at_level("INFO"):
        spool.flush(CONFIG)

    assert "1 report(s) delivered, 1 discarded" in caplog.text
    assert spool.depth() == 0
