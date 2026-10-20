"""Python packages, per system interpreter.

`scope` is the interpreter's resolved path rather than a constant, because one
device can hold the same package at different versions under two interpreters.
Collapsing them would make every report flap the version back and forth.
"""

import json
import logging
import os
import shutil
from pathlib import Path

from sentinel_watchdog.collectors import CollectorSkipped, run

log = logging.getLogger("sentinel.collectors.python")

# System interpreters only. Virtualenvs and per-user pyenv installs inside other
# users' home directories are not readable by the unprivileged watchdog user, and
# walking the filesystem for them is explicitly out of scope for v1.
SEARCH_DIRS = ("/usr/bin", "/usr/local/bin")
CANDIDATE_PREFIX = "python3"


def find_interpreters() -> list[str]:
    """Distinct system python3 interpreters, deduplicated by real path."""
    found: dict[str, str] = {}
    for directory in SEARCH_DIRS:
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.startswith(CANDIDATE_PREFIX):
                continue
            # Skip python3-config, python3.11-config and similar helpers.
            suffix = entry[len(CANDIDATE_PREFIX):]
            if suffix and not suffix.replace(".", "").isdigit():
                continue
            path = Path(directory) / entry
            if not path.is_file() or not os.access(path, os.X_OK):
                continue
            real = str(path.resolve())
            found.setdefault(real, real)
    return sorted(found)


def parse(stdout: str, scope: str) -> list[dict]:
    try:
        entries = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CollectorSkipped(f"pip returned non-JSON output: {exc}") from exc
    if not isinstance(entries, list):
        raise CollectorSkipped("pip returned an unexpected JSON shape")

    packages: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        version = str(entry.get("version", "")).strip()
        if not name or not version:
            continue
        packages.append(
            {
                "ecosystem": "python",
                "name": name,
                "version": version,
                "scope": scope,
                "meta": {},
            }
        )
    return packages


def collect() -> list[dict]:
    interpreters = find_interpreters()
    if not interpreters:
        raise CollectorSkipped("no system python3 interpreter found")

    packages: list[dict] = []
    reported: list[str] = []
    for interpreter in interpreters:
        try:
            stdout = run([interpreter, "-m", "pip", "list", "--format=json"], timeout=60)
            # parse() must be inside the try: it raises CollectorSkipped on
            # unparseable output, and letting that escape would throw away the
            # packages already collected from earlier interpreters.
            found = parse(stdout, scope=interpreter)
        except CollectorSkipped as exc:
            # pip missing or broken for one interpreter is normal; keep going.
            log.info("skipping %s: %s", interpreter, exc)
            continue
        packages.extend(found)
        reported.append(interpreter)

    if not reported:
        raise CollectorSkipped(
            f"pip is not available for any of: {', '.join(interpreters)}"
        )
    return packages


def available() -> bool:
    return shutil.which("python3") is not None
