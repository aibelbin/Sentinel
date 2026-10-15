"""Filesystem locations, overridable by environment variable so tests can run
against a temporary directory without touching the real install."""

import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SENTINEL_CONFIG", "/etc/sentinel-agent/config.json"))
STATE_DIR = Path(os.environ.get("SENTINEL_STATE_DIR", "/var/lib/sentinel-agent"))
LOG_DIR = Path(os.environ.get("SENTINEL_LOG_DIR", "/var/log/sentinel-agent"))

# Where the agent's own code lives.
#
#   <root>/bin/sentinel-agent   stable launcher, root-owned, never replaced
#   <root>/live/versions/<id>/  agent-owned, replaced by updates
#   <root>/live/current         symlink the launcher follows
#
# The writable area is a subdirectory rather than the root itself: write
# permission on a directory lets you delete or rename anything in it whatever
# its owner, so an agent-writable root would let the agent replace the
# launcher -- which the installer and `sudo sentinel-agent` run as root.
INSTALL_ROOT = Path(os.environ.get("SENTINEL_INSTALL_ROOT", "/opt/sentinel-agent"))
LIVE_DIR = INSTALL_ROOT / "live"
VERSIONS_DIR = LIVE_DIR / "versions"
CURRENT_LINK = LIVE_DIR / "current"

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
