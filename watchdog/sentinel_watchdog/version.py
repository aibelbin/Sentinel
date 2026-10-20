"""What version this watchdog is running.

The deployed identity is assigned by the server: it hashes its copy of the
watchdog source, so any edit produces a new version with no number to bump and no
way to forget. On disk that hash is the name of the directory the code lives
in, which makes the running version self-describing — read the `current`
symlink and you know exactly what is executing.

`__version__` in __init__.py stays as a human-facing release string and is only
used when the watchdog is not running from a versioned install, which is the case
in tests and when running from a source checkout.
"""

from pathlib import Path

from sentinel_watchdog import __version__, paths

# Reported when the code is not laid out as versions/<id>/sentinel_watchdog.
DEV_VERSION = f"dev-{__version__}"


def running_version() -> str:
    """The version id of the code currently executing."""
    # .../live/versions/<id>/sentinel_watchdog/version.py -> <id>
    here = Path(__file__).resolve()
    if here.parents[2].name == paths.VERSIONS_DIR.name:
        return here.parents[1].name
    return DEV_VERSION


def installed_version() -> str | None:
    """The version `current` points at, which may differ from what is running
    if a swap happened during this process's lifetime."""
    try:
        return paths.CURRENT_LINK.resolve(strict=True).name
    except OSError:
        return None


def is_versioned_install() -> bool:
    return running_version() != DEV_VERSION
