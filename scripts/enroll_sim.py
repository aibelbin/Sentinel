#!/usr/bin/env python3
"""Enroll the simulated devices, the way an administrator would by hand.

Logs in, creates a device per sim container (or rotates its key if it already
exists), then runs the real installer inside the container. Standard library
only, so it runs on the host with no setup.

    python3 scripts/enroll_sim.py
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = os.environ.get("SENTINEL_API", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-now")

# The URL the watchdog uses from inside the Compose network, which is not the one
# the host uses to reach the API.
INTERNAL_API = os.environ.get("SENTINEL_INTERNAL_API", "http://api:8000")

SIM_CONTAINERS = {
    "sim-device-1": "sim-web-01",
    "sim-device-2": "sim-db-01",
}


def request(method: str, path: str, token: str | None = None, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if body:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(f"{API}/api/v1{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {path} failed: HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"cannot reach {API}: {exc}\nIs the stack up? Try: make up"
        ) from exc


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *args], check=False, text=True)


def main() -> int:
    print(f"==> signing in to {API} as {ADMIN_USER}")
    session = request("POST", "/auth/login", payload={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    token = session["token"]

    existing = {device["name"]: device for device in request("GET", "/devices", token=token)}

    for container, device_name in SIM_CONTAINERS.items():
        print(f"\n==> {container} -> device '{device_name}'")

        if device_name in existing:
            device_id = existing[device_name]["id"]
            print("    device exists, rotating its key")
            issued = request("POST", f"/devices/{device_id}/rotate-key", token=token)
        else:
            issued = request("POST", "/devices", token=token, payload={"name": device_name})
            print(f"    created device {issued['device_id']}")

        print("    running the installer inside the container")
        result = compose(
            "exec",
            "-T",
            container,
            "bash",
            "/opt/sentinel-src/install.sh",
            "--server",
            INTERNAL_API,
            "--key",
            issued["api_key"],
            "--insecure",
            "--from-source",
            "/opt/sentinel-src",
            "--force-reenroll",
        )
        if result.returncode != 0:
            print(f"    installer failed in {container} (exit {result.returncode})", file=sys.stderr)
            return 1

    print("\n==> done. Open the dashboard; the devices report within a minute.")
    print("    To see inventory immediately: make sim-collect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
