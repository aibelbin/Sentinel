"""Software inventory collectors.

Each collector returns a list of {ecosystem, name, version, scope, meta}. A
collector that cannot run is not an error: it returns nothing and records why,
so a report is submitted with whatever succeeded rather than being lost
entirely. The dashboard shows which collectors failed.
"""

import logging
import shutil
import subprocess

log = logging.getLogger("sentinel.collectors")

DEFAULT_TIMEOUT = 30


class CollectorSkipped(Exception):
    """The collector cannot run here, which is a normal condition."""


def run(argv: list[str], timeout: int = DEFAULT_TIMEOUT, allow_nonzero: bool = False) -> str:
    """Run a command and return stdout.

    Never uses a shell, so no value can be interpreted as shell syntax.

    allow_nonzero is for tools that report a non-zero exit alongside perfectly
    usable output -- `npm ls -g` does this whenever the global tree has unmet
    peer dependencies, which is common and not a reason to report nothing.
    """
    if shutil.which(argv[0]) is None:
        raise CollectorSkipped(f"{argv[0]} is not installed")

    try:
        completed = subprocess.run(  # noqa: S603 - argv list, shell=False
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectorSkipped(f"{argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise CollectorSkipped(f"{argv[0]} failed to start: {exc}") from exc

    if completed.returncode != 0 and not (allow_nonzero and completed.stdout.strip()):
        stderr = (completed.stderr or "").strip().splitlines()
        hint = stderr[-1][:160] if stderr else f"exit {completed.returncode}"
        raise CollectorSkipped(f"{argv[0]} exited {completed.returncode}: {hint}")

    return completed.stdout


def collect_all() -> tuple[list[dict], dict[str, str]]:
    """Run every collector. Returns (packages, per-collector status)."""
    from sentinel_watchdog.collectors import java_pkgs, npm_pkgs, os_pkgs, python_pkgs

    collectors = {
        "os": os_pkgs.collect,
        "python": python_pkgs.collect,
        "npm": npm_pkgs.collect,
        "java": java_pkgs.collect,
    }

    packages: list[dict] = []
    status: dict[str, str] = {}
    for name, func in collectors.items():
        try:
            found = func()
        except CollectorSkipped as exc:
            status[name] = f"skipped: {exc}"
            log.info("collector %s skipped: %s", name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - one collector must not sink the report
            status[name] = f"failed: {type(exc).__name__}: {exc}"
            log.exception("collector %s failed", name)
            continue

        packages.extend(found)
        status[name] = f"ok: {len(found)}"

    return packages, status
