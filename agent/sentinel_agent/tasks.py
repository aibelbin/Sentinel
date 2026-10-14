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
