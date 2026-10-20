"""Enrollment credentials, stored mode 600 and readable only by the watchdog user."""

import json
import logging
import os
import pwd
import tempfile
from dataclasses import asdict, dataclass

from sentinel_watchdog import paths

log = logging.getLogger("sentinel.config")

# The unprivileged account cron runs the watchdog as. Enrolment is usually done
# with sudo, so the file it writes has to end up readable by this account
# rather than by root.
WATCHDOG_USER = "sentinel"


class NotEnrolled(Exception):
    pass


@dataclass
class Config:
    server_url: str
    api_key: str
    device_id: str | None = None
    insecure: bool = False

    @property
    def base_url(self) -> str:
        return self.server_url.rstrip("/")


def load() -> Config:
    try:
        raw = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotEnrolled(f"not enrolled: {paths.CONFIG_PATH} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise NotEnrolled(f"config at {paths.CONFIG_PATH} is not valid JSON: {exc}") from exc

    missing = [field for field in ("server_url", "api_key") if not raw.get(field)]
    if missing:
        raise NotEnrolled(f"config is missing {', '.join(missing)}")

    return Config(
        server_url=raw["server_url"],
        api_key=raw["api_key"],
        device_id=raw.get("device_id"),
        insecure=bool(raw.get("insecure", False)),
    )


def _hand_to_watchdog_user(fd: int) -> None:
    """Give the file to the account that will actually read it.

    Enrolment is normally run with sudo, which would otherwise leave a
    root-owned mode-600 config that the unprivileged cron job cannot open --
    the watchdog would install cleanly and then fail every beat with a permission
    error. Only meaningful when running as root; otherwise the file already
    belongs to whoever wrote it.
    """
    if os.geteuid() != 0:
        return
    try:
        account = pwd.getpwnam(WATCHDOG_USER)
    except KeyError:
        # No such account, which means this is not a packaged install.
        return
    try:
        os.fchown(fd, account.pw_uid, account.pw_gid)
    except OSError as exc:
        log.warning("could not hand %s to %s: %s", paths.CONFIG_PATH, WATCHDOG_USER, exc)


def hand_path_to_watchdog_user(path) -> None:
    """Give a path to the account the watchdog runs as, when running as root.

    Enrolment is normally `sudo sentinel-watchdog enroll`, so anything it creates
    would otherwise be root-owned and unwritable by the unprivileged cron job.
    """
    if os.geteuid() != 0:
        return
    try:
        account = pwd.getpwnam(WATCHDOG_USER)
    except KeyError:
        return
    try:
        os.chown(path, account.pw_uid, account.pw_gid)
    except OSError as exc:
        log.warning("could not hand %s to %s: %s", path, WATCHDOG_USER, exc)


def save(config: Config) -> None:
    """Write atomically, and never leave the key world-readable.

    The temporary file is created in the destination directory with mode 600
    before any content is written, so the key is never briefly readable.
    """
    paths.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=paths.CONFIG_PATH.parent, prefix=".config-")
    try:
        os.fchmod(fd, 0o600)
        _hand_to_watchdog_user(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(config), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, paths.CONFIG_PATH)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
