"""What Liva can do. Each function maps to endpoints the dashboard already uses.

Functions return ToolOutput: `text` is what the model sees (never a secret);
`ui` is an optional card for the administrator's screen only.
"""

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any

from .config import get_settings
from .security import mask_key
from .sentinel import Sentinel, SentinelError

MAX_RESULT_CHARS = 12000

_DEVICE_FIELDS = (
    "id", "name", "status", "enabled", "hostname", "os_name", "os_version",
    "package_count", "last_seen_at", "watchdog_version", "update_status",
)


class ToolError(Exception):
    """A problem the model should be told about, in plain words."""


@dataclass
class ToolOutput:
    text: str
    ui: dict | None = None


def clip(value: Any) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (
        text[:MAX_RESULT_CHARS]
        + f"... [truncated, {len(text)} chars in total; narrow the query or use offset]"
    )


async def resolve_device(c: Sentinel, ref: str) -> str:
    """Accepts a UUID, or a device name the administrator would actually say."""
    try:
        return str(uuid.UUID(ref))
    except ValueError:
        pass
    matches = await c.get("/devices", q=ref)
    exact = [d for d in matches if d["name"].lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(matches) == 1:
        return matches[0]["id"]
    if not matches:
        raise ToolError(f"no device matches {ref!r}")
    names = ", ".join(sorted(d["name"] for d in matches))
    raise ToolError(f"{ref!r} is ambiguous; it matches: {names}")


def _slim(device: dict) -> dict:
    out = {key: device.get(key) for key in _DEVICE_FIELDS}
    res = device.get("resources")
    if res:
        total, available = res.get("memory_total_bytes"), res.get("memory_available_bytes")
        used = round((1 - available / total) * 100, 1) if total and available is not None else None
        out["resources"] = {
            "cpu_percent": res.get("cpu_percent"),
            "memory_used_percent": used,
            "load_1": res.get("load_1"),
            "uptime_seconds": res.get("uptime_seconds"),
        }
    return out


# ---- reads ----------------------------------------------------------------- #


async def list_devices(c: Sentinel, status: str | None = None, q: str | None = None) -> ToolOutput:
    devices = await c.get("/devices", status=status, q=q)
    return ToolOutput(clip({"count": len(devices), "devices": [_slim(d) for d in devices]}))


async def get_device(c: Sentinel, device: str) -> ToolOutput:
    device_id = await resolve_device(c, device)
    return ToolOutput(clip(await c.get(f"/devices/{device_id}")))


async def list_packages(
    c: Sentinel, device: str, ecosystem: str | None = None, q: str | None = None,
    limit: int = 50, offset: int = 0,
) -> ToolOutput:
    device_id = await resolve_device(c, device)
    page = await c.get(
        f"/devices/{device_id}/packages", ecosystem=ecosystem, q=q, limit=limit, offset=offset
    )
    return ToolOutput(clip(page))


async def list_changes(c: Sentinel, device: str, limit: int = 50, offset: int = 0) -> ToolOutput:
    device_id = await resolve_device(c, device)
    return ToolOutput(clip(await c.get(f"/devices/{device_id}/changes", limit=limit, offset=offset)))


async def list_reports(c: Sentinel, device: str, limit: int = 20, offset: int = 0) -> ToolOutput:
    device_id = await resolve_device(c, device)
    return ToolOutput(clip(await c.get(f"/devices/{device_id}/reports", limit=limit, offset=offset)))


async def list_tasks(c: Sentinel, device: str, limit: int = 20, offset: int = 0) -> ToolOutput:
    device_id = await resolve_device(c, device)
    return ToolOutput(clip(await c.get(f"/devices/{device_id}/tasks", limit=limit, offset=offset)))


async def get_config(c: Sentinel) -> ToolOutput:
    return ToolOutput(clip(await c.get("/config")))


async def get_release_status(c: Sentinel) -> ToolOutput:
    return ToolOutput(clip(await c.get("/watchdog-release")))


# ---- credentials ----------------------------------------------------------- #


def _file(name: str, mime: str, data: bytes) -> dict:
    return {"name": name, "mime": mime, "data_b64": base64.b64encode(data).decode()}


async def _package(c: Sentinel) -> tuple[str, bytes]:
    release = await c.get("/watchdog-release")
    return release["package_filename"], await c.get_bytes("/watchdog-package")


async def _credentials_card(c: Sentinel, device_name: str, key: str, *, with_package: bool) -> dict:
    server = get_settings().public_url
    insecure = server.startswith("http://")
    files: list[dict] = []
    warnings: list[str] = []
    package_name: str | None = None

    # A failed build must not lose the key: it is shown once and cannot be
    # recovered, so every step after issuing it is best-effort.
    if with_package:
        try:
            package_name, data = await _package(c)
            files.append(_file(package_name, "application/vnd.debian.binary-package", data))
        except SentinelError as exc:
            package_name = None
            warnings.append(f"Could not build the .deb: {exc.message}")
    try:
        script = await c.post_text(
            "/installer", {"server_url": server, "api_key": key, "insecure": insecure}
        )
        files.append(_file("sentinel-install.sh", "text/x-shellscript", script.encode()))
    except SentinelError as exc:
        warnings.append(f"Could not build the installer script: {exc.message}")

    enroll = f"sudo sentinel-watchdog enroll --server {server} --key {key}" + (
        " --insecure" if insecure else ""
    )
    commands = ([f"sudo apt install ./{package_name}"] if package_name else []) + [enroll]
    return {
        "kind": "credentials", "device": device_name, "api_key": key,
        "commands": commands, "files": files, "warnings": warnings,
    }


async def create_device(c: Sentinel, name: str) -> ToolOutput:
    issued = await c.post("/devices", {"name": name})
    card = await _credentials_card(c, issued["name"], issued["api_key"], with_package=True)
    text = (
        f"Device '{issued['name']}' created (id {issued['device_id']}). "
        f"API key issued: {mask_key(issued['api_key'])}. The full key, the .deb, the install "
        "script and the enroll commands are now shown to the administrator on screen. "
        "Do not try to repeat the key; tell them it is shown once."
    )
    return ToolOutput(text, card)


async def rotate_device_key(c: Sentinel, device: str) -> ToolOutput:
    device_id = await resolve_device(c, device)
    detail = await c.get(f"/devices/{device_id}")
    issued = await c.post(f"/devices/{device_id}/rotate-key")
    card = await _credentials_card(c, detail["name"], issued["api_key"], with_package=False)
    text = (
        f"Key for '{detail['name']}' rotated; new key {mask_key(issued['api_key'])}. The device stops "
        "reporting until re-enrolled. The full key and enroll command are shown to the "
        "administrator on screen; do not repeat the key."
    )
    return ToolOutput(text, card)


async def build_package(c: Sentinel) -> ToolOutput:
    name, data = await _package(c)
    card = {"kind": "download", "files": [_file(name, "application/vnd.debian.binary-package", data)]}
    return ToolOutput(
        f"Package {name} built ({len(data)} bytes) and offered to the administrator as a download.", card
    )


# ---- writes ---------------------------------------------------------------- #


async def update_device(
    c: Sentinel, device: str, name: str | None = None, enabled: bool | None = None
) -> ToolOutput:
    payload = {k: v for k, v in (("name", name), ("enabled", enabled)) if v is not None}
    if not payload:
        raise ToolError("nothing to change: pass a new name and/or enabled")
    device_id = await resolve_device(c, device)
    return ToolOutput(clip(_slim(await c.patch(f"/devices/{device_id}", payload))))


async def queue_collect_inventory(c: Sentinel, device: str) -> ToolOutput:
    device_id = await resolve_device(c, device)
    task = await c.post(f"/devices/{device_id}/tasks", {"type": "collect_inventory"})
    return ToolOutput(clip(task))


async def delete_device(c: Sentinel, device: str) -> ToolOutput:
    device_id = await resolve_device(c, device)
    detail = await c.get(f"/devices/{device_id}")
    await c.delete(f"/devices/{device_id}")
    return ToolOutput(f"Device '{detail['name']}' ({device_id}) deleted.")


async def update_config(
    c: Sentinel,
    poll_interval_seconds: int | None = None,
    inventory_interval_seconds: int | None = None,
    spool_max_bytes: int | None = None,
) -> ToolOutput:
    changes = {
        k: v for k, v in (
            ("poll_interval_seconds", poll_interval_seconds),
            ("inventory_interval_seconds", inventory_interval_seconds),
            ("spool_max_bytes", spool_max_bytes),
        ) if v is not None
    }
    if not changes:
        raise ToolError("nothing to change: pass at least one setting")
    merged = {**await c.get("/config"), **changes}
    return ToolOutput(clip(await c.put("/config", merged)))


async def clear_quarantine(c: Sentinel) -> ToolOutput:
    return ToolOutput(clip(await c.post("/watchdog-release/clear-quarantine")))
