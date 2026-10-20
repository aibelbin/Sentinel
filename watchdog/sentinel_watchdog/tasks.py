"""Task dispatch.

Security boundary: the server sends a task *type*, never a command. The type is
looked up in a fixed registry of local functions, and anything unrecognized is
reported failed rather than ignored or guessed at. No value from the server is
ever passed to a shell, interpolated into a command, or evaluated.
"""

import logging
from collections.abc import Callable

log = logging.getLogger("sentinel.tasks")

# type -> (handler, allowed parameter names)
_REGISTRY: dict[str, tuple[Callable[..., dict], frozenset[str]]] = {}


def handler(task_type: str, allowed_params: frozenset[str] = frozenset()):
    def decorator(func: Callable[..., dict]) -> Callable[..., dict]:
        _REGISTRY[task_type] = (func, allowed_params)
        return func

    return decorator


class UnsupportedTask(Exception):
    pass


def dispatch(task_type: str, params: dict, context: dict) -> dict:
    """Run the handler for task_type and return its result payload."""
    entry = _REGISTRY.get(task_type)
    if entry is None:
        raise UnsupportedTask(f"unsupported task type: {task_type!r}")

    func, allowed = entry
    if not isinstance(params, dict):
        raise UnsupportedTask("task params must be an object")

    unexpected = set(params) - allowed
    if unexpected:
        raise UnsupportedTask(f"unexpected parameters: {', '.join(sorted(unexpected))}")

    return func(params, context)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


@handler("collect_inventory", allowed_params=frozenset())
def _collect_inventory(_params: dict, context: dict) -> dict:
    """Collect and submit inventory immediately, ignoring the schedule."""
    collect = context["collect_now"]
    outcome = collect(reason="task")
    return {
        "package_count": outcome.get("package_count", 0),
        "change_count": outcome.get("change_count"),
        "spooled": outcome.get("spooled", False),
    }


@handler("update_watchdog", allowed_params=frozenset({"version", "sha256"}))
def _update_watchdog(params: dict, context: dict) -> dict:
    """Install the watchdog version the server says this machine should run.

    The version and checksum are the only things taken from the server, and
    both are checked against the bundle before anything is written. No path,
    URL or command comes from the job.
    """
    version = params.get("version")
    sha256 = params.get("sha256")
    if not isinstance(version, str) or not version:
        raise UnsupportedTask("update_watchdog requires a version")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise UnsupportedTask("update_watchdog requires a 64-character sha256")
    if not all(character in "0123456789abcdefABCDEF" for character in sha256):
        raise UnsupportedTask("sha256 is not hexadecimal")
    # The version names a directory, so it must not be able to traverse.
    if not all(character.isalnum() or character in "-._" for character in version):
        raise UnsupportedTask("version contains unexpected characters")
    if version.startswith(".") or "/" in version:
        raise UnsupportedTask("version is not a safe directory name")

    return context["install_update"](version=version, sha256=sha256.lower())
