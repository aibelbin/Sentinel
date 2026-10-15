"""Filesystem locations, overridable by environment variable so tests can run
against a temporary directory without touching the real install."""

import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SENTINEL_CONFIG", "/etc/sentinel-agent/config.json"))
STATE_DIR = Path(os.environ.get("SENTINEL_STATE_DIR", "/var/lib/sentinel-agent"))
LOG_DIR = Path(os.environ.get("SENTINEL_LOG_DIR", "/var/log/sentinel-agent"))

# Where the agent's own code lives. `bin/sentinel-agent` is the stable
# entrypoint and is never replaced; everything under versions/ is.
INSTALL_ROOT = Path(os.environ.get("SENTINEL_INSTALL_ROOT", "/opt/sentinel-agent"))
VERSIONS_DIR = INSTALL_ROOT / "versions"
CURRENT_LINK = INSTALL_ROOT / "current"

STATE_DB = STATE_DIR / "state.db"
LOCK_PATH = STATE_DIR / "agent.lock"
LOG_PATH = LOG_DIR / "agent.log"

# Written when a new version is swapped in, cleared once that version has
# completed a beat. Its presence means "this version is still on probation".
PENDING_VERSION_PATH = STATE_DIR / "pending_version.json"

# Where collectors look for the host's /proc. Overridable so collector tests
# can point at a fixture tree.
PROC_DIR = Path(os.environ.get("SENTINEL_PROC_DIR", "/proc"))
OS_RELEASE_PATH = Path(os.environ.get("SENTINEL_OS_RELEASE", "/etc/os-release"))
JVM_DIR = Path(os.environ.get("SENTINEL_JVM_DIR", "/usr/lib/jvm"))
