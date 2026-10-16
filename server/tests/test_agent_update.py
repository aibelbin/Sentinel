"""Agent self-update: version identity, handing it out, and quarantine."""

import base64
import hashlib
import io
import tarfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import AgentRelease, Task, TaskStatus
from app.services import agent_release
from app.services.installer import clear_caches


@pytest.fixture(autouse=True)
def _fresh_caches():
    agent_release.clear_caches()
    clear_caches()
    yield
    agent_release.clear_caches()
    clear_caches()


@pytest.fixture
def agent_source(tmp_path, monkeypatch):
    """A minimal agent source tree, so tests do not depend on the real one."""
    root = tmp_path / "agent"
    (root / "sentinel_agent").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "sentinel_agent" / "cli.py").write_text("def main(argv=None):\n    return 0\n")
    (root / "bin" / "sentinel-agent").write_text("#!/usr/bin/env python3\n")
    (root / "install.sh").write_text(
        "SERVER_URL='@@SERVER_URL@@'\nAPI_KEY='@@API_KEY@@'\nINSECURE='@@INSECURE@@'\n"
        "AGENT_VERSION='@@AGENT_VERSION@@'\nAGENT_BUNDLE_B64='@@BUNDLE@@'\n"
        "LAUNCHER_B64='@@LAUNCHER@@'\n"
    )

    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_SOURCE_DIR", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def heartbeat(client, device_auth, version: str, **extra):
    body = {"agent_version": version, **extra}
    return client.post("/api/v1/agent/heartbeat", json=body, headers=device_auth)


# --------------------------------------------------------------------------- #
# Version identity
# --------------------------------------------------------------------------- #


def test_version_is_derived_from_the_source(agent_source):
    version, sha256 = agent_release.current_release()
    assert len(version) == agent_release.VERSION_LENGTH
    assert len(sha256) == 64


def test_editing_the_agent_changes_the_version(agent_source):
    """No version number to bump: an edit is the version change."""
    before, before_sha = agent_release.current_release()

    agent_release.clear_caches()
    clear_caches()
    (agent_source / "sentinel_agent" / "collector.py").write_text("# a new feature\n")

    after, after_sha = agent_release.current_release()
    assert after != before
    assert after_sha != before_sha


def test_moving_a_file_changes_the_version(agent_source):
    """Paths are hashed with contents, so a rename counts as a change even
    when the bytes are identical."""
    before, _ = agent_release.current_release()
    agent_release.clear_caches()
    clear_caches()
    (agent_source / "sentinel_agent" / "cli.py").rename(
        agent_source / "sentinel_agent" / "main.py"
    )
    assert agent_release.current_release()[0] != before


def test_version_is_stable_when_nothing_changes(agent_source):
    first, _ = agent_release.current_release()
    agent_release.clear_caches()
    clear_caches()
    assert agent_release.current_release()[0] == first


def test_the_launcher_is_not_in_the_update_bundle(agent_source):
    """Updates must never replace the one piece that can revert a bad version."""
    from app.services.installer import bundle_bytes, launcher_bytes

    with tarfile.open(fileobj=io.BytesIO(bundle_bytes(str(agent_source))), mode="r:gz") as tar:
        bundle_names = tar.getnames()
    with tarfile.open(fileobj=io.BytesIO(launcher_bytes(str(agent_source))), mode="r:gz") as tar:
        launcher_names = tar.getnames()

    assert not any(name.startswith("bin") for name in bundle_names)
    assert any("sentinel_agent" in name for name in bundle_names)
    assert "bin/sentinel-agent" in launcher_names


# --------------------------------------------------------------------------- #
# Handing updates out
# --------------------------------------------------------------------------- #


def test_a_machine_on_an_old_version_is_offered_an_update(
    client, auth, device_key, device_auth, agent_source, db
):
    device_id, _ = device_key
    response = heartbeat(client, device_auth, "0000deadbeef")
    assert response.status_code == 200

    tasks = response.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["type"] == "update_agent"
    assert tasks[0]["params"]["version"] == agent_release.current_release()[0]
    assert len(tasks[0]["params"]["sha256"]) == 64


def test_a_machine_on_the_current_version_is_left_alone(
    client, device_auth, agent_source
):
    version, _ = agent_release.current_release()
    response = heartbeat(client, device_auth, version)
    assert response.json()["tasks"] == []


def test_only_one_update_is_in_flight_at_a_time(client, device_auth, agent_source, db):
    """Without this, every heartbeat would pile on another update job."""
    heartbeat(client, device_auth, "0000deadbeef")
    heartbeat(client, device_auth, "0000deadbeef")
    heartbeat(client, device_auth, "0000deadbeef")

    tasks = db.execute(select(Task).where(Task.type == "update_agent")).scalars().all()
    assert len(tasks) == 1


def test_a_dev_install_is_never_offered_an_update(client, device_auth, agent_source):
    """An agent running from a source checkout has no versioned layout to
    swap, so pushing code at it would only fail."""
    response = heartbeat(client, device_auth, "dev-1.0.0")
    assert response.json()["tasks"] == []


def test_a_disabled_device_is_not_offered_an_update(
    client, auth, device_key, device_auth, agent_source
):
    device_id, _ = device_key
    client.patch(f"/api/v1/devices/{device_id}", json={"enabled": False}, headers=auth)
    assert heartbeat(client, device_auth, "0000deadbeef").status_code == 403


# --------------------------------------------------------------------------- #
# The bundle endpoint
# --------------------------------------------------------------------------- #


def test_bundle_checksum_matches_what_the_job_promised(
    client, device_auth, agent_source
):
    version, sha256 = agent_release.current_release()
    response = client.get(
        f"/api/v1/agent/bundle?version={version}", headers=device_auth
    )
    assert response.status_code == 200

    payload = response.json()
    raw = base64.b64decode(payload["bundle_b64"])
    assert hashlib.sha256(raw).hexdigest() == sha256 == payload["sha256"]
    # It has to be a real archive, not just bytes with a matching hash.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        assert any(name.endswith("cli.py") for name in tar.getnames())


def test_bundle_refuses_a_stale_version(client, device_auth, agent_source):
    response = client.get("/api/v1/agent/bundle?version=0000deadbeef", headers=device_auth)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "version_not_current"


def test_bundle_needs_a_device_key(client, auth, agent_source):
    version, _ = agent_release.current_release()
    assert client.get(f"/api/v1/agent/bundle?version={version}").status_code == 401
    # An admin token is not a device credential.
    assert client.get(f"/api/v1/agent/bundle?version={version}", headers=auth).status_code == 403


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #


def test_a_failed_update_quarantines_the_version(
    client, auth, device_key, device_auth, agent_source, db
):
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()

    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "failed", "error": "self-test failed: boom"},
        headers=device_auth,
    )

    version, _ = agent_release.current_release()
    release = db.get(AgentRelease, version)
    db.refresh(release)
    assert release.quarantined_at is not None
    assert "boom" in release.failure_reason


def test_a_quarantined_version_is_not_offered_to_anyone_else(
    client, auth, device_key, device_auth, agent_source, db
):
    """The whole point: one machine has a bad minute instead of the fleet."""
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "failed", "error": "broken"},
        headers=device_auth,
    )

    other = client.post("/api/v1/devices", json={"name": "second"}, headers=auth).json()
    other_auth = {"Authorization": f"Bearer {other['api_key']}"}
    assert heartbeat(client, other_auth, "0000deadbeef").json()["tasks"] == []


def test_a_successful_update_does_not_quarantine(
    client, device_auth, agent_source, db
):
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "completed", "result": {"installed": True}},
        headers=device_auth,
    )
    version, _ = agent_release.current_release()
    release = db.get(AgentRelease, version)
    assert release.quarantined_at is None


def test_a_machine_is_not_retried_forever_after_failing(
    client, device_auth, agent_source, db
):
    """One attempt per version per machine, or a bad build would be retried
    every single minute."""
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "failed", "error": "broken"},
        headers=device_auth,
    )
    # Lift the quarantine without stamping a retry: clearing it through the
    # normal path deliberately lifts the per-machine guard too, which is the
    # thing under test here.
    version, _ = agent_release.current_release()
    release = db.get(AgentRelease, version)
    db.refresh(release)
    release.quarantined_at = None
    db.commit()

    heartbeat(client, device_auth, "0000deadbeef")
    count = len(db.execute(select(Task).where(Task.type == "update_agent")).scalars().all())
    assert count == 1


def test_clearing_quarantine_allows_a_fresh_attempt(
    client, auth, device_auth, agent_source, db
):
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "failed", "error": "flaky network"},
        headers=device_auth,
    )

    # The failing attempt has to predate the retry marker for the guard to lift.
    task.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()

    cleared = client.post("/api/v1/agent-release/clear-quarantine", headers=auth)
    assert cleared.status_code == 200
    assert cleared.json()["quarantined"] is False

    assert len(heartbeat(client, device_auth, "0000deadbeef").json()["tasks"]) == 1


def test_clearing_a_healthy_version_is_a_conflict(client, auth, agent_source):
    response = client.post("/api/v1/agent-release/clear-quarantine", headers=auth)
    assert response.status_code == 409


def test_deploying_a_new_version_sidesteps_the_quarantine(
    client, device_auth, agent_source, db
):
    """Quarantine is tied to one hash, so shipping a fix clears it by itself."""
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "failed", "error": "broken"},
        headers=device_auth,
    )

    agent_release.clear_caches()
    clear_caches()
    (agent_source / "sentinel_agent" / "fixed.py").write_text("# the fix\n")

    tasks = heartbeat(client, device_auth, "0000deadbeef").json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["params"]["version"] == agent_release.current_release()[0]


# --------------------------------------------------------------------------- #
# What the dashboard shows
# --------------------------------------------------------------------------- #


def test_update_status_reflects_the_machine(
    client, auth, device_key, device_auth, agent_source
):
    device_id, _ = device_key
    version, _ = agent_release.current_release()

    heartbeat(client, device_auth, "0000deadbeef")
    assert client.get(f"/api/v1/devices/{device_id}", headers=auth).json()[
        "update_status"
    ] == "update pending"

    heartbeat(client, device_auth, version)
    assert client.get(f"/api/v1/devices/{device_id}", headers=auth).json()[
        "update_status"
    ] == "up to date"


def test_a_revert_is_surfaced(client, auth, device_key, device_auth, agent_source):
    """Only the machine knows it rolled back, so it has to be reported."""
    device_id, _ = device_key
    version, _ = agent_release.current_release()
    heartbeat(
        client,
        device_auth,
        "0000deadbeef",
        update={
            "result": "reverted",
            "detail": f"{version} failed to run; fell back to 0000deadbeef",
            "version": version,
        },
    )

    detail = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()
    assert detail["update_status"] == "rolled back"
    assert detail["last_update_result"] == "reverted"
    assert "fell back" in detail["last_update_detail"]
    assert detail["previous_agent_version"] == version


def test_steady_state_ok_does_not_keep_moving_the_timestamp(
    client, auth, device_key, device_auth, agent_source, db
):
    """Agents report "ok" on every beat once proven; last_update_at has to keep
    meaning when the update happened."""
    device_id, _ = device_key
    report = {"result": "ok", "detail": "confirmed abc", "version": "abc"}
    heartbeat(client, device_auth, "0000deadbeef", update=report)

    first = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()["last_update_at"]
    heartbeat(client, device_auth, "0000deadbeef", update=report)
    second = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()["last_update_at"]
    assert first == second


def test_release_summary_counts_the_fleet(client, auth, device_auth, agent_source):
    version, _ = agent_release.current_release()
    heartbeat(client, device_auth, version)

    summary = client.get("/api/v1/agent-release", headers=auth).json()
    assert summary["version"] == version
    assert summary["devices_on_version"] == 1
    assert summary["devices_total"] == 1
    assert summary["quarantined"] is False


def test_release_summary_needs_auth(client, agent_source):
    assert client.get("/api/v1/agent-release").status_code == 401


# --------------------------------------------------------------------------- #
# Bootstrapping: an agent from before self-updating existed
# --------------------------------------------------------------------------- #


def test_a_legacy_agent_is_not_offered_an_update(client, device_auth, agent_source):
    """An agent reporting a release string rather than a hash predates the
    mechanism: it has no handler and no versioned layout."""
    assert heartbeat(client, device_auth, "1.0.0").json()["tasks"] == []


def test_a_device_that_never_reported_is_not_offered_an_update(
    client, auth, agent_source, db
):
    created = client.post("/api/v1/devices", json={"name": "never-seen"}, headers=auth).json()
    device = db.get(__import__("app.models", fromlist=["Device"]).Device, created["device_id"])
    assert agent_release.maybe_queue_update(db, device) is None


def test_a_legacy_agent_cannot_quarantine_a_release(
    client, auth, device_key, device_auth, agent_source, db
):
    """The bug this guards against: an old agent rejects the job it cannot
    understand, and that failure took the release away from the whole fleet."""
    version, sha256 = agent_release.current_release()
    agent_release.record(db)
    db.commit()

    # Simulate what an old agent does with a job type it does not know.
    task = Task(
        device_id=device_key[0],
        type="update_agent",
        params={"version": version, "sha256": sha256},
        status=TaskStatus.running,
    )
    db.add(task)
    db.commit()

    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "failed", "error": "unsupported task type: 'update_agent'"},
        headers=device_auth,
    )

    release = db.get(AgentRelease, version)
    db.refresh(release)
    assert release.quarantined_at is None

    # And a current agent still gets offered the version.
    other = client.post("/api/v1/devices", json={"name": "modern"}, headers=auth).json()
    other_auth = {"Authorization": f"Bearer {other['api_key']}"}
    assert len(heartbeat(client, other_auth, "0000deadbeef").json()["tasks"]) == 1


def test_a_real_build_failure_still_quarantines(
    client, device_auth, agent_source, db
):
    """The unsupported-job exemption must not swallow genuine failures."""
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={"status": "failed", "error": "self-test failed: cannot import collectors"},
        headers=device_auth,
    )
    version, _ = agent_release.current_release()
    release = db.get(AgentRelease, version)
    db.refresh(release)
    assert release.quarantined_at is not None


def test_status_says_not_managed_rather_than_held_back(
    client, auth, device_key, device_auth, agent_source, db
):
    """A legacy machine needs the installer; what the fleet version is doing is
    not the useful thing to show for it."""
    device_id, _ = device_key
    version, _ = agent_release.current_release()
    agent_release.record(db)
    agent_release.quarantine(db, version, None, "something broke")
    db.commit()

    heartbeat(client, device_auth, "1.0.0")
    detail = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()
    assert detail["update_status"] == "not managed"


# --------------------------------------------------------------------------- #
# A version that installs and then cannot run
# --------------------------------------------------------------------------- #


def test_a_reverted_version_is_quarantined(
    client, auth, device_key, device_auth, agent_source, db
):
    """The swap succeeded, so the job was reported completed. A revert is the
    only signal that the version could not actually run, and without acting on
    it the server kept re-pushing the same broken build."""
    version, _ = agent_release.current_release()
    heartbeat(client, device_auth, "0000deadbeef")

    heartbeat(
        client, device_auth, "0000deadbeef",
        update={"result": "reverted", "detail": "never completed a beat",
                "version": version},
    )

    release = db.get(AgentRelease, version)
    db.refresh(release)
    assert release.quarantined_at is not None
    assert "never completed a beat" in release.failure_reason


def test_a_machine_that_reverted_is_not_offered_the_same_version_again(
    client, auth, device_key, device_auth, agent_source, db
):
    version, _ = agent_release.current_release()
    heartbeat(
        client, device_auth, "0000deadbeef",
        update={"result": "reverted", "detail": "crashed", "version": version},
    )
    # Quarantine lifted, so only the per-machine guard is under test.
    release = db.get(AgentRelease, version)
    db.refresh(release)
    release.quarantined_at = None
    db.commit()

    assert heartbeat(client, device_auth, "0000deadbeef").json()["tasks"] == []


def test_a_revert_of_an_older_version_does_not_block_the_current_one(
    client, device_auth, agent_source, db
):
    """A machine that rolled back once must still receive later versions."""
    # The same heartbeat that carries the report also delivers the job.
    tasks = heartbeat(
        client, device_auth, "0000deadbeef",
        update={"result": "reverted", "detail": "old failure", "version": "999999999999"},
    ).json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["params"]["version"] == agent_release.current_release()[0]


def test_rolled_back_status_is_not_sticky(
    client, auth, device_key, device_auth, agent_source
):
    device_id, _ = device_key
    heartbeat(
        client, device_auth, "0000deadbeef",
        update={"result": "reverted", "detail": "old failure", "version": "999999999999"},
    )
    detail = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()
    # The revert was about a version no longer being offered, so the useful
    # state is that this machine is behind.
    assert detail["update_status"] == "update pending"


# --------------------------------------------------------------------------- #
# Only a build failure withholds a version from the fleet
# --------------------------------------------------------------------------- #


def test_a_fetch_failure_does_not_quarantine(
    client, device_auth, agent_source, db
):
    """One machine's network blip must not take a healthy release away from
    everyone else."""
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={
            "status": "failed",
            "result": {"kind": "transient"},
            "error": "could not fetch the bundle: timed out",
        },
        headers=device_auth,
    )
    version, _ = agent_release.current_release()
    release = db.get(AgentRelease, version)
    db.refresh(release)
    assert release.quarantined_at is None


def test_a_build_failure_still_quarantines_when_typed(
    client, device_auth, agent_source, db
):
    heartbeat(client, device_auth, "0000deadbeef")
    task = db.execute(select(Task).where(Task.type == "update_agent")).scalar_one()
    client.post(
        f"/api/v1/agent/tasks/{task.id}/result",
        json={
            "status": "failed",
            "result": {"kind": "build"},
            "error": "self-test failed: cannot import collectors",
        },
        headers=device_auth,
    )
    version, _ = agent_release.current_release()
    release = db.get(AgentRelease, version)
    db.refresh(release)
    assert release.quarantined_at is not None


@pytest.mark.parametrize(
    ("kind", "error", "expected"),
    [
        ("build", "self-test failed", True),
        ("transient", "timed out", False),
        ("unsupported", "unsupported task type", False),
        (None, "checksum mismatch", True),
        (None, "could not fetch the bundle: timed out", False),
        (None, "unsupported task type: 'update_agent'", False),
    ],
)
def test_build_failure_classification(kind, error, expected):
    assert agent_release.is_build_failure(kind, error) is expected


# --------------------------------------------------------------------------- #
# The release row under a fleet-wide first heartbeat
# --------------------------------------------------------------------------- #


def test_recording_a_release_twice_is_safe(agent_source, db):
    """After a deploy the whole fleet races to insert the same row; the loser
    used to raise UniqueViolation and 500 the heartbeat."""
    first = agent_release.record(db)
    db.commit()
    second = agent_release.record(db)
    db.commit()
    assert first.version == second.version


def test_release_summary_names_the_package(client, auth, agent_source):
    """The dashboard offers the .deb for download, so it needs the filename
    from the code that builds it rather than reconstructing the scheme."""
    from app.services import debian_package

    version, _ = agent_release.current_release()
    summary = client.get("/api/v1/agent-release", headers=auth).json()

    assert summary["package_filename"] == debian_package.filename(version)
    assert summary["package_filename"].startswith("sentinel-agent_")
    assert summary["package_filename"].endswith("_all.deb")
    assert version in summary["package_filename"]
