"""Enrollment credentials, stored mode 600 and readable only by the agent user."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass

from sentinel_agent import paths


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


def save(config: Config) -> None:
    """Write atomically, and never leave the key world-readable.

    The temporary file is created in the destination directory with mode 600
    before any content is written, so the key is never briefly readable.
    """
    paths.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=paths.CONFIG_PATH.parent, prefix=".config-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(config), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, paths.CONFIG_PATH)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
