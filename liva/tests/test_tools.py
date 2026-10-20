import asyncio
import base64
import json

import httpx
import pytest

from app import tools
from app.sentinel import Sentinel, SentinelError

KEY = "sk_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
DEVICE_ID = "11111111-1111-1111-1111-111111111111"


def make_client(routes):
    """routes: {(METHOD, path): (status, body)}; body dict/list -> json, bytes/str raw."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        calls.append((request.method, path, request.url.params, request.content))
        status, body = routes.get((request.method, path), (404, {"detail": {"code": "nope"}}))
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, content=body)

    return Sentinel("tok", "http://api:8000", transport=httpx.MockTransport(handler)), calls


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_URL", "https://sentinel.example")


DEVICE = {
    "id": DEVICE_ID, "name": "web-01", "status": "online", "enabled": True,
    "hostname": "web-01", "os_name": "Ubuntu", "os_version": "24.04", "package_count": 812,
    "last_seen_at": "2026-10-21T10:00:00Z", "watchdog_version": "abc", "update_status": "up to date",
    "resources": {"cpu_percent": 12.5, "memory_total_bytes": 1000, "memory_available_bytes": 250,
                  "load_1": 0.5, "uptime_seconds": 99, "disks": []},
}


def test_list_devices_slims_the_payload_and_passes_filters():
    client, calls = make_client({("GET", "/devices"): (200, [DEVICE])})
    out = run(tools.list_devices(client, status="online", q="web"))
    data = json.loads(out.text)
    assert data["count"] == 1
    assert data["devices"][0]["resources"]["memory_used_percent"] == 75.0
    assert "disks" not in data["devices"][0]["resources"]
    assert dict(calls[0][2]) == {"status": "online", "q": "web"}


def test_device_reference_accepts_a_name_and_resolves_it():
    client, calls = make_client({
        ("GET", "/devices"): (200, [DEVICE]),
        ("GET", f"/devices/{DEVICE_ID}"): (200, DEVICE),
    })
    out = run(tools.get_device(client, "WEB-01"))
    assert json.loads(out.text)["name"] == "web-01"


def test_ambiguous_or_missing_device_is_a_tool_error():
    other = {**DEVICE, "id": "22222222-2222-2222-2222-222222222222", "name": "web-02"}
    client, _ = make_client({("GET", "/devices"): (200, [DEVICE, other])})
    with pytest.raises(tools.ToolError, match="ambiguous"):
        run(tools.get_device(client, "web"))
    client, _ = make_client({("GET", "/devices"): (200, [])})
    with pytest.raises(tools.ToolError, match="no device"):
        run(tools.get_device(client, "ghost"))


def test_list_packages_forwards_paging():
    page = {"items": [], "total": 0, "limit": 50, "offset": 10}
    client, calls = make_client({("GET", f"/devices/{DEVICE_ID}/packages"): (200, page)})
    run(tools.list_packages(client, DEVICE_ID, ecosystem="python", q="req", limit=50, offset=10))
    assert dict(calls[0][2]) == {"ecosystem": "python", "q": "req", "limit": "50", "offset": "10"}


def test_results_are_clipped_to_a_safe_size():
    big = [{"name": f"pkg-{i}", "version": "1" * 50} for i in range(2000)]
    client, _ = make_client({("GET", f"/devices/{DEVICE_ID}/packages"): (200, {"items": big})})
    out = run(tools.list_packages(client, DEVICE_ID))
    assert len(out.text) <= tools.MAX_RESULT_CHARS + 200
    assert "truncated" in out.text


def test_create_device_keeps_the_key_out_of_the_model_text():
    release = {"package_filename": "sentinel-watchdog_1.0.0+abc_all.deb"}
    client, calls = make_client({
        ("POST", "/devices"): (201, {"device_id": DEVICE_ID, "name": "web-01", "api_key": KEY}),
        ("GET", "/watchdog-release"): (200, release),
        ("GET", "/watchdog-package"): (200, b"DEBBYTES"),
        ("POST", "/installer"): (200, b"#!/bin/sh\necho hi\n"),
    })
    out = run(tools.create_device(client, "web-01"))

    assert KEY not in out.text
    assert "sk_dev_…2345" in out.text
    card = out.ui
    assert card["kind"] == "credentials" and card["api_key"] == KEY
    assert card["commands"][0] == "sudo apt install ./sentinel-watchdog_1.0.0+abc_all.deb"
    assert card["commands"][1] == f"sudo sentinel-watchdog enroll --server https://sentinel.example --key {KEY}"
    names = {f["name"]: f for f in card["files"]}
    assert base64.b64decode(names["sentinel-watchdog_1.0.0+abc_all.deb"]["data_b64"]) == b"DEBBYTES"
    assert "sentinel-install.sh" in names
    installer_call = [c for c in calls if c[1] == "/installer"][0]
    assert json.loads(installer_call[3]) == {
        "server_url": "https://sentinel.example", "api_key": KEY, "insecure": False}


def test_create_device_still_returns_the_key_if_the_package_build_fails():
    client, _ = make_client({
        ("POST", "/devices"): (201, {"device_id": DEVICE_ID, "name": "web-01", "api_key": KEY}),
        ("GET", "/watchdog-release"): (200, {"package_filename": "x.deb"}),
        ("GET", "/watchdog-package"): (500, {"detail": {"code": "package_unavailable", "message": "no dpkg"}}),
        ("POST", "/installer"): (200, b"#!/bin/sh\n"),
    })
    out = run(tools.create_device(client, "web-01"))
    assert out.ui["api_key"] == KEY
    assert any("no dpkg" in w for w in out.ui["warnings"])
    assert out.ui["commands"] == [f"sudo sentinel-watchdog enroll --server https://sentinel.example --key {KEY}"]


def test_http_public_url_adds_insecure_flag(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_URL", "http://10.0.0.5:8000")
    client, _ = make_client({
        ("POST", "/devices"): (201, {"device_id": DEVICE_ID, "name": "w", "api_key": KEY}),
        ("GET", "/watchdog-release"): (200, {"package_filename": "x.deb"}),
        ("GET", "/watchdog-package"): (200, b"D"),
        ("POST", "/installer"): (200, b"#!/bin/sh\n"),
    })
    out = run(tools.create_device(client, "w"))
    assert out.ui["commands"][-1].endswith(" --insecure")


def test_rotate_device_key_returns_a_card_without_a_package():
    client, _ = make_client({
        ("GET", f"/devices/{DEVICE_ID}"): (200, DEVICE),
        ("POST", f"/devices/{DEVICE_ID}/rotate-key"): (200, {"device_id": DEVICE_ID, "name": "web-01", "api_key": KEY}),
        ("POST", "/installer"): (200, b"#!/bin/sh\n"),
    })
    out = run(tools.rotate_device_key(client, DEVICE_ID))
    assert KEY not in out.text
    assert out.ui["kind"] == "credentials"
    assert [f["name"] for f in out.ui["files"]] == ["sentinel-install.sh"]


def test_build_package_offers_a_download_card():
    client, _ = make_client({
        ("GET", "/watchdog-release"): (200, {"package_filename": "p.deb"}),
        ("GET", "/watchdog-package"): (200, b"DEB"),
    })
    out = run(tools.build_package(client))
    assert out.ui["kind"] == "download"
    assert out.ui["files"][0]["name"] == "p.deb"
    assert "p.deb" in out.text


def test_update_device_requires_a_change_and_patches():
    client, calls = make_client({
        ("PATCH", f"/devices/{DEVICE_ID}"): (200, {**DEVICE, "enabled": False}),
    })
    with pytest.raises(tools.ToolError):
        run(tools.update_device(client, DEVICE_ID))
    run(tools.update_device(client, DEVICE_ID, enabled=False))
    assert json.loads(calls[-1][3]) == {"enabled": False}


def test_queue_collect_inventory_posts_the_only_task_type():
    client, calls = make_client({
        ("POST", f"/devices/{DEVICE_ID}/tasks"): (201, {"id": "t1", "type": "collect_inventory", "status": "queued"}),
    })
    run(tools.queue_collect_inventory(client, DEVICE_ID))
    assert json.loads(calls[0][3]) == {"type": "collect_inventory"}


def test_delete_device_looks_up_the_name_then_deletes():
    client, calls = make_client({
        ("GET", f"/devices/{DEVICE_ID}"): (200, DEVICE),
        ("DELETE", f"/devices/{DEVICE_ID}"): (204, b""),
    })
    out = run(tools.delete_device(client, DEVICE_ID))
    assert "web-01" in out.text
    assert calls[-1][0] == "DELETE"


def test_update_config_merges_with_current_values():
    current = {"poll_interval_seconds": 60, "inventory_interval_seconds": 3600, "spool_max_bytes": 5_000_000}
    client, calls = make_client({
        ("GET", "/config"): (200, current),
        ("PUT", "/config"): (200, {**current, "poll_interval_seconds": 120}),
    })
    run(tools.update_config(client, poll_interval_seconds=120))
    assert json.loads(calls[-1][3]) == {**current, "poll_interval_seconds": 120}
    with pytest.raises(tools.ToolError):
        run(tools.update_config(client))


def test_sentinel_error_carries_the_servers_message():
    client, _ = make_client({
        ("GET", "/config"): (500, {"detail": {"code": "boom", "message": "database is down"}}),
    })
    with pytest.raises(SentinelError, match="database is down"):
        run(tools.get_config(client))


# ---- regressions from review ------------------------------------------------ #


def test_destructive_tools_refuse_a_loose_name_match():
    client, calls = make_client({("GET", "/devices"): (200, [{**DEVICE, "name": "web-prod-01"}])})
    with pytest.raises(tools.ToolError, match="exact"):
        run(tools.delete_device(client, "web"))
    with pytest.raises(tools.ToolError, match="web-prod-01"):
        run(tools.rotate_device_key(client, "web"))
    assert all(c[0] == "GET" for c in calls)


def test_destructive_tools_accept_an_exact_name():
    client, _ = make_client({
        ("GET", "/devices"): (200, [DEVICE]),
        ("GET", f"/devices/{DEVICE_ID}"): (200, DEVICE),
        ("DELETE", f"/devices/{DEVICE_ID}"): (204, b""),
    })
    assert "web-01" in run(tools.delete_device(client, "WEB-01")).text


def test_the_key_survives_an_unexpected_failure_building_extras():
    client, _ = make_client({
        ("POST", "/devices"): (201, {"device_id": DEVICE_ID, "name": "web-01", "api_key": KEY}),
        ("GET", "/watchdog-release"): (200, {"unexpected": "shape"}),   # no package_filename
        ("POST", "/installer"): (200, b"#!/bin/sh\n"),
    })
    out = run(tools.create_device(client, "web-01"))
    assert out.ui["api_key"] == KEY
    assert out.ui["warnings"]
