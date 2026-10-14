"""Globally-installed npm packages.

Global only: project-level node_modules scanning needs configurable roots,
depth limits and exclusion rules, and is deferred to a later version.
"""

import json

from sentinel_agent.collectors import CollectorSkipped, run


def parse(stdout: str) -> list[dict]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CollectorSkipped(f"npm returned non-JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectorSkipped("npm returned an unexpected JSON shape")

    dependencies = payload.get("dependencies") or {}
    if not isinstance(dependencies, dict):
        return []

    packages: list[dict] = []
    for name, info in dependencies.items():
        version = ""
        if isinstance(info, dict):
            version = str(info.get("version", "")).strip()
        if not name or not version:
            continue
        packages.append(
            {
                "ecosystem": "npm",
                "name": name,
                "version": version,
                "scope": "system",
                "meta": {},
            }
        )
    return packages


def collect() -> list[dict]:
    stdout = run(
        ["npm", "ls", "-g", "--depth=0", "--json"], timeout=90, allow_nonzero=True
    )
    return parse(stdout)
