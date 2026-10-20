"""Host and OS facts.

These are metadata only. The platform never uses them for identity or to merge
device history -- that is what the device ID and its API key are for.
"""

import logging
import os
import platform

from sentinel_watchdog import paths

log = logging.getLogger("sentinel.collectors.host")


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _cpu_facts() -> tuple[str | None, int | None]:
    """Model name and physical core count from /proc/cpuinfo."""
    try:
        text = (paths.PROC_DIR / "cpuinfo").read_text(encoding="utf-8")
    except OSError:
        return None, os.cpu_count()

    model: str | None = None
    processors = 0
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "model name" and model is None:
            model = value[:255]
        elif key == "processor":
            processors += 1
        elif key == "Model" and model is None:
            # ARM boards report "Model" rather than "model name".
            model = value[:255]

    return model, processors or os.cpu_count()


def _total_memory() -> int | None:
    try:
        text = (paths.PROC_DIR / "meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def facts() -> dict:
    os_release: dict[str, str] = {}
    try:
        os_release = _parse_os_release(paths.OS_RELEASE_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        log.debug("cannot read %s: %s", paths.OS_RELEASE_PATH, exc)

    cpu_model, cpu_cores = _cpu_facts()

    return {
        "hostname": platform.node()[:255] or None,
        "os_name": (os_release.get("NAME") or platform.system())[:128] or None,
        "os_version": (os_release.get("VERSION_ID") or "")[:64] or None,
        "kernel": platform.release()[:128] or None,
        "arch": platform.machine()[:32] or None,
        "cpu_model": cpu_model,
        "cpu_cores": cpu_cores,
        "total_memory_bytes": _total_memory(),
    }
