"""End-to-end API flow and the authorization boundaries around it."""

from datetime import datetime, timedelta, timezone

HOST = {
    "hostname": "web-01",
    "os_name": "Ubuntu",
    "os_version": "22.04",
    "kernel": "5.15.0-91-generic",
    "arch": "x86_64",
    "cpu_model": "Intel(R) Xeon(R) CPU @ 2.20GHz",
    "cpu_cores": 4,
    "total_memory_bytes": 8203431936,
}

RESOURCES = {
    "cpu_percent": 12.5,
    "memory_total_bytes": 8203431936,
    "memory_available_bytes": 4120094720,
    "load_1": 0.15,
    "load_5": 0.22,
    "load_15": 0.18,
    "uptime_seconds": 98234,
    "disks": [
        {"mountpoint": "/", "filesystem": "ext4", "total_bytes": 50_000_000_000,
         "free_bytes": 20_000_000_000}
    ],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_login_rejects_a_wrong_password(client, admin_token):
    response = client.post(
        "/api/v1/auth/login", json={"username": "tester", "password": "wrong"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"


def test_login_rejects_an_unknown_user(client):
    response = client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert response.status_code == 401


def test_admin_routes_require_a_token(client):
    assert client.get("/api/v1/devices").status_code == 401


def test_a_device_key_cannot_be_used_as_an_admin_token(client, device_key):
    """The two credential types share a header; they must not share authority."""
    response = client.get(
        "/api/v1/devices", headers={"Authorization": f"Bearer {device_key[1]}"}
    )
    assert response.status_code == 401


def test_an_admin_token_cannot_be_used_as_a_device_key(client, admin_token):
    response = client.post(
        "/api/v1/agent/heartbeat",
        json={"agent_version": "1.0.0"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 403


def test_logout_invalidates_the_session(client, auth):
    assert client.post("/api/v1/auth/logout", headers=auth).status_code == 204
    assert client.get("/api/v1/devices", headers=auth).status_code == 401


# --------------------------------------------------------------------------- #
# Device creation and keys
# --------------------------------------------------------------------------- #


def test_create_device_returns_the_key_once(client, auth):
    created = client.post("/api/v1/devices", json={"name": "web-01"}, headers=auth).json()
    assert created["api_key"].startswith("sk_dev_")

    detail = client.get(f"/api/v1/devices/{created['device_id']}", headers=auth).json()
    # The plaintext must not be recoverable from any later read.
    assert "api_key" not in detail


def test_duplicate_device_name_is_a_conflict(client, auth):
    client.post("/api/v1/devices", json={"name": "web-01"}, headers=auth)
    response = client.post("/api/v1/devices", json={"name": "web-01"}, headers=auth)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "duplicate_name"


def test_rotate_key_revokes_the_old_one(client, auth, device_key):
    device_id, old_key = device_key
    new_key = client.post(f"/api/v1/devices/{device_id}/rotate-key", headers=auth).json()["api_key"]
    assert new_key != old_key

    old = client.post(
        "/api/v1/agent/heartbeat",
        json={"agent_version": "1.0.0"},
        headers={"Authorization": f"Bearer {old_key}"},
    )
    assert old.status_code == 403
    assert old.json()["detail"]["code"] == "invalid_or_revoked_key"

    new = client.post(
        "/api/v1/agent/heartbeat",
        json={"agent_version": "1.0.0"},
        headers={"Authorization": f"Bearer {new_key}"},
    )
    assert new.status_code == 200


def test_disabled_device_is_refused_with_a_distinct_code(client, auth, device_key, device_auth):
    device_id, _ = device_key
    client.patch(f"/api/v1/devices/{device_id}", json={"enabled": False}, headers=auth)

    response = client.post(
        "/api/v1/agent/heartbeat", json={"agent_version": "1.0.0"}, headers=device_auth
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "device_disabled"


def test_disabled_device_reports_disabled_status(client, auth, device_key, device_auth):
    device_id, _ = device_key
    client.post("/api/v1/agent/heartbeat", json={"agent_version": "1.0.0"}, headers=device_auth)
    client.patch(f"/api/v1/devices/{device_id}", json={"enabled": False}, headers=auth)

    detail = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()
    assert detail["status"] == "disabled"


# --------------------------------------------------------------------------- #
# Agent lifecycle
# --------------------------------------------------------------------------- #


def test_enroll_returns_identity_and_config(client, device_auth):
    response = client.post(
        "/api/v1/agent/enroll",
        json={"agent_version": "1.0.0", "host": HOST},
        headers=device_auth,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "test-device"
    assert body["config"]["poll_interval_seconds"] == 60
    assert body["config"]["inventory_interval_seconds"] == 43200


def test_enroll_is_idempotent(client, device_auth):
    first = client.post(
        "/api/v1/agent/enroll", json={"agent_version": "1.0.0", "host": HOST}, headers=device_auth
    ).json()
    second = client.post(
        "/api/v1/agent/enroll", json={"agent_version": "1.0.1", "host": HOST}, headers=device_auth
    ).json()
    assert first["device_id"] == second["device_id"]


def test_heartbeat_records_resources_and_goes_online(client, auth, device_key, device_auth):
    device_id, _ = device_key
    response = client.post(
        "/api/v1/agent/heartbeat",
        json={"agent_version": "1.0.0", "resources": RESOURCES, "host": HOST},
        headers=device_auth,
    )
    assert response.status_code == 200

    detail = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()
    assert detail["status"] == "online"
    assert detail["resources"]["cpu_percent"] == 12.5
    assert detail["resources"]["disks"][0]["mountpoint"] == "/"
    assert detail["cpu_model"] == HOST["cpu_model"]


def test_first_beat_may_report_unknown_cpu(client, auth, device_key, device_auth):
    """The agent has no prior /proc/stat sample on its first run, and null must
    survive the round trip rather than becoming 0."""
    device_id, _ = device_key
    client.post(
        "/api/v1/agent/heartbeat",
        json={"agent_version": "1.0.0", "resources": {**RESOURCES, "cpu_percent": None}},
        headers=device_auth,
    )
    detail = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()
    assert detail["resources"]["cpu_percent"] is None


def test_inventory_round_trip(client, auth, device_key, device_auth):
    device_id, _ = device_key
    response = client.post(
        "/api/v1/agent/inventory",
        json={
            "collected_at": now_iso(),
            "agent_version": "1.0.0",
            "collector_status": {"os": "ok: 2", "npm": "skipped: npm is not installed"},
            "packages": [
                {"ecosystem": "os", "name": "nginx", "version": "1.24.0"},
                {"ecosystem": "python", "name": "requests", "version": "2.31.0",
                 "scope": "/usr/bin/python3.10"},
            ],
        },
        headers=device_auth,
    )
    assert response.status_code == 200
    assert response.json()["change_count"] == 2

    packages = client.get(f"/api/v1/devices/{device_id}/packages", headers=auth).json()
    assert packages["total"] == 2

    detail = client.get(f"/api/v1/devices/{device_id}", headers=auth).json()
    assert detail["package_count"] == 2
    # Partial collection is surfaced, not hidden.
    assert "skipped" in detail["last_collector_status"]["npm"]


def test_package_filters(client, auth, device_key, device_auth):
    device_id, _ = device_key
    client.post(
        "/api/v1/agent/inventory",
        json={
            "collected_at": now_iso(),
            "agent_version": "1.0.0",
            "packages": [
                {"ecosystem": "os", "name": "nginx", "version": "1.24.0"},
                {"ecosystem": "python", "name": "requests", "version": "2.31.0"},
            ],
        },
        headers=device_auth,
    )

    by_eco = client.get(
        f"/api/v1/devices/{device_id}/packages", params={"ecosystem": "python"}, headers=auth
    ).json()
    assert by_eco["total"] == 1

    by_name = client.get(
        f"/api/v1/devices/{device_id}/packages", params={"q": "ngin"}, headers=auth
    ).json()
    assert by_name["total"] == 1

    bad = client.get(
        f"/api/v1/devices/{device_id}/packages", params={"ecosystem": "cargo"}, headers=auth
    )
    assert bad.status_code == 400


def test_oversized_report_is_rejected(client, device_auth):
    response = client.post(
        "/api/v1/agent/inventory",
        json={
            "collected_at": now_iso(),
            "agent_version": "1.0.0",
            "packages": [
                {"ecosystem": "os", "name": f"pkg-{index}", "version": "1.0"}
                for index in range(20001)
            ],
        },
        headers=device_auth,
    )
    assert response.status_code == 422


def test_unknown_fields_are_rejected(client, device_auth):
    """extra="forbid" means a protocol drift is a loud error, not silence."""
    response = client.post(
        "/api/v1/agent/heartbeat",
        json={"agent_version": "1.0.0", "unexpected_field": True},
        headers=device_auth,
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


def test_task_is_delivered_once_then_marked_running(client, auth, device_key, device_auth):
    device_id, _ = device_key
    client.post(
        f"/api/v1/devices/{device_id}/tasks", json={"type": "collect_inventory"}, headers=auth
    )

    first = client.post(
        "/api/v1/agent/heartbeat", json={"agent_version": "1.0.0"}, headers=device_auth
    ).json()
    assert len(first["tasks"]) == 1
    task_id = first["tasks"][0]["id"]

    # Claimed on delivery, so the next beat does not run it again.
    second = client.post(
        "/api/v1/agent/heartbeat", json={"agent_version": "1.0.0"}, headers=device_auth
    ).json()
    assert second["tasks"] == []

    client.post(
        f"/api/v1/agent/tasks/{task_id}/result",
        json={"status": "completed", "result": {"package_count": 42}},
        headers=device_auth,
    )
    tasks = client.get(f"/api/v1/devices/{device_id}/tasks", headers=auth).json()
    assert tasks["items"][0]["status"] == "completed"
    assert tasks["items"][0]["result"]["package_count"] == 42


def test_a_device_cannot_post_a_result_for_another_devices_task(client, auth, device_key):
    device_id, _ = device_key
    other = client.post("/api/v1/devices", json={"name": "other"}, headers=auth).json()

    queued = client.post(
        f"/api/v1/devices/{device_id}/tasks", json={"type": "collect_inventory"}, headers=auth
    ).json()

    response = client.post(
        f"/api/v1/agent/tasks/{queued['id']}/result",
        json={"status": "completed", "result": {}},
        headers={"Authorization": f"Bearer {other['api_key']}"},
    )
    # 404 rather than 403, so a device cannot probe for other devices' task IDs.
    assert response.status_code == 404


def test_unknown_task_type_is_refused_at_the_api(client, auth, device_key):
    device_id, _ = device_key
    response = client.post(
        f"/api/v1/devices/{device_id}/tasks", json={"type": "rm -rf /"}, headers=auth
    )
    assert response.status_code == 422


def test_queueing_for_a_disabled_device_is_refused(client, auth, device_key):
    device_id, _ = device_key
    client.patch(f"/api/v1/devices/{device_id}", json={"enabled": False}, headers=auth)
    response = client.post(
        f"/api/v1/devices/{device_id}/tasks", json={"type": "collect_inventory"}, headers=auth
    )
    assert response.status_code == 409


def test_abandoned_task_is_flagged(client, auth, device_key, device_auth, db):
    from app.models import Task

    device_id, _ = device_key
    client.post(
        f"/api/v1/devices/{device_id}/tasks", json={"type": "collect_inventory"}, headers=auth
    )
    client.post("/api/v1/agent/heartbeat", json={"agent_version": "1.0.0"}, headers=device_auth)

    task = db.query(Task).one()
    task.claimed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()

    tasks = client.get(f"/api/v1/devices/{device_id}/tasks", headers=auth).json()
    assert tasks["items"][0]["abandoned"] is True


# --------------------------------------------------------------------------- #
# Config and installer
# --------------------------------------------------------------------------- #


def test_config_update_reaches_the_agent(client, auth, device_auth):
    client.put(
        "/api/v1/config",
        json={
            "poll_interval_seconds": 120,
            "inventory_interval_seconds": 21600,
            "spool_max_bytes": 33554432,
        },
        headers=auth,
    )
    beat = client.post(
        "/api/v1/agent/heartbeat", json={"agent_version": "1.0.0"}, headers=device_auth
    ).json()
    assert beat["config"]["poll_interval_seconds"] == 120
    assert beat["config"]["inventory_interval_seconds"] == 21600


def test_config_rejects_out_of_range_values(client, auth):
    response = client.put(
        "/api/v1/config",
        json={
            "poll_interval_seconds": 1,
            "inventory_interval_seconds": 21600,
            "spool_max_bytes": 33554432,
        },
        headers=auth,
    )
    assert response.status_code == 422


def test_installer_download_requires_auth(client):
    assert client.get("/api/v1/installer").status_code == 401


def test_generic_installer_has_no_credentials(client, auth):
    response = client.get("/api/v1/installer", headers=auth)
    assert response.status_code == 200
    script = response.text
    assert "SERVER_URL=''" in script
    assert "API_KEY=''" in script
    assert "@@BUNDLE@@" not in script  # the bundle was substituted in


def test_prefilled_installer_embeds_the_credentials(client, auth, device_key):
    _, api_key = device_key
    response = client.post(
        "/api/v1/installer",
        json={"server_url": "https://sentinel.example", "api_key": api_key, "insecure": False},
        headers=auth,
    )
    assert response.status_code == 200
    script = response.text
    assert f"SERVER_URL='https://sentinel.example'" in script
    assert f"API_KEY='{api_key}'" in script
    assert "attachment" in response.headers["content-disposition"]


def test_installer_bundle_unpacks_to_a_working_agent(client, auth, tmp_path):
    """The downloaded script must actually carry the agent, not just claim to."""
    import base64
    import io
    import re
    import tarfile

    script = client.get("/api/v1/installer", headers=auth).text
    match = re.search(r"AGENT_BUNDLE_B64='([A-Za-z0-9+/=]+)'", script)
    assert match, "no embedded bundle found in the installer"

    raw = base64.b64decode(match.group(1))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = tar.getnames()

    assert "bin/sentinel-agent" in names
    assert "sentinel_agent/cli.py" in names
    assert "sentinel_agent/collectors/os_pkgs.py" in names
    assert not any(name.endswith(".pyc") for name in names)


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #


def test_deleting_a_device_removes_its_data(client, auth, device_key, device_auth):
    device_id, api_key = device_key
    client.post(
        "/api/v1/agent/inventory",
        json={
            "collected_at": now_iso(),
            "agent_version": "1.0.0",
            "packages": [{"ecosystem": "os", "name": "nginx", "version": "1.24.0"}],
        },
        headers=device_auth,
    )

    assert client.delete(f"/api/v1/devices/{device_id}", headers=auth).status_code == 204
    assert client.get(f"/api/v1/devices/{device_id}", headers=auth).status_code == 404

    # Its key must stop working too, or a deleted device could still report.
    response = client.post(
        "/api/v1/agent/heartbeat",
        json={"agent_version": "1.0.0"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 403


def test_healthz_is_unauthenticated(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["database"] is True
