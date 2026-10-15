"""Which agent version machines should be running, and whether it is safe.

The version is a hash of the server's own copy of the agent source, so any
edit produces a new version with no number to bump and no way to forget. The
deployed code is the source of truth: reverting the checkout and redeploying
moves the fleet back, which makes `git revert` a working emergency brake.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AgentRelease, Device, Task, TaskStatus
from app.services.installer import BUNDLE_MEMBERS, bundle_bytes, source_fingerprint

log = logging.getLogger(__name__)

UPDATE_TASK_TYPE = "update_agent"
VERSION_LENGTH = 12

# A version id is a source hash. An agent reporting anything else -- a release
# string like "1.0.0", or nothing at all -- predates self-updating, so it has
# no handler for the job and no versioned layout to swap. Reporting a
# hash-shaped version is what marks an agent as manageable.
_VERSION_SHAPE = re.compile(rf"^[0-9a-f]{{{VERSION_LENGTH}}}$")


def manages_updates(agent_version: str | None) -> bool:
    return bool(agent_version) and bool(_VERSION_SHAPE.match(agent_version))


@lru_cache(maxsize=4)
def _version_for(agent_source_dir: str, _fingerprint: tuple) -> tuple[str, str]:
    """Return (version, sha256) for the agent source as it stands on disk.

    The version identifies the *source*; the checksum covers the *bundle* the
    agent will actually download, so the two are computed together and stay
    consistent with each other.
    """
    root = Path(agent_source_dir)
    digest = hashlib.sha256()
    for name in sorted(BUNDLE_MEMBERS):
        base = root / name
        for path in sorted(base.rglob("*") if base.is_dir() else [base], key=str):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            # The path is hashed alongside the content so that moving a file
            # counts as a change.
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())

    version = digest.hexdigest()[:VERSION_LENGTH]
    bundle_sha = hashlib.sha256(bundle_bytes(agent_source_dir)).hexdigest()
    return version, bundle_sha


def current_release() -> tuple[str, str]:
    """(version, bundle sha256) for the agent source this server is serving."""
    source_dir = get_settings().agent_source_dir
    return _version_for(source_dir, source_fingerprint(Path(source_dir)))


def record(db: Session) -> AgentRelease:
    """Make sure the current version has a row, and return it."""
    version, sha256 = current_release()
    row = db.get(AgentRelease, version)
    if row is None:
        row = AgentRelease(version=version, sha256=sha256)
        db.add(row)
        db.flush()
        log.info("agent release %s is now current", version)
    elif row.sha256 != sha256:
        # Same source hash but a different bundle means the packaging changed
        # underneath us; the checksum agents verify against has to follow.
        row.sha256 = sha256
        db.flush()
    return row


# An agent that cannot understand the job is not evidence of a bad build, so
# this failure must not quarantine the release.
UNSUPPORTED_MARKERS = ("unsupported task type", "unexpected parameters")


def quarantine(db: Session, version: str, device_id, reason: str) -> None:
    """Stop handing out a version after a machine reported it failed."""
    lowered = (reason or "").lower()
    if any(marker in lowered for marker in UNSUPPORTED_MARKERS):
        log.info("not quarantining %s: the agent did not understand the job", version)
        return

    row = db.get(AgentRelease, version)
    if row is None or row.quarantined_at is not None:
        return
    row.quarantined_at = datetime.now(timezone.utc)
    row.failure_reason = (reason or "")[:1000] or "no reason given"
    row.failure_device_id = device_id
    db.flush()
    log.warning("quarantined agent release %s: %s", version, row.failure_reason)


def clear_quarantine(db: Session, version: str) -> bool:
    row = db.get(AgentRelease, version)
    if row is None or row.quarantined_at is None:
        return False
    row.quarantined_at = None
    row.failure_reason = None
    row.failure_device_id = None
    row.retry_requested_at = datetime.now(timezone.utc)
    db.flush()
    log.info("cleared quarantine on agent release %s", version)
    return True


def maybe_queue_update(db: Session, device: Device) -> Task | None:
    """Queue an update for a machine running something other than the current
    version.

    Called from the heartbeat, so it has to be cheap and must never queue a
    duplicate while one is already in flight.
    """
    release = record(db)
    if release.quarantined_at is not None:
        return None
    if not device.enabled:
        return None
    if device.agent_version == release.version:
        return None
    # An agent from before self-updating existed cannot apply the job, and
    # would report it failed -- which previously quarantined the very version
    # being rolled out, blocking it for the whole fleet. Those machines need
    # the installer run once and are left alone until then.
    if not manages_updates(device.agent_version):
        return None

    existing = db.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.device_id == device.id,
            Task.type == UPDATE_TASK_TYPE,
            Task.status.in_((TaskStatus.pending, TaskStatus.running)),
        )
    ).scalar_one()
    if existing:
        return None

    # One attempt per version per machine: without this, a version that fails
    # on a machine would be re-queued on its very next heartbeat, forever.
    tried = select(func.count()).select_from(Task).where(
        Task.device_id == device.id,
        Task.type == UPDATE_TASK_TYPE,
        Task.status == TaskStatus.failed,
        Task.params["version"].astext == release.version,
    )
    if release.retry_requested_at is not None:
        # Only failures since the operator asked for a retry count against it.
        tried = tried.where(Task.created_at > release.retry_requested_at)
    if db.execute(tried).scalar_one():
        return None

    task = Task(
        device_id=device.id,
        type=UPDATE_TASK_TYPE,
        params={"version": release.version, "sha256": release.sha256},
    )
    db.add(task)
    db.flush()
    log.info("queued update to %s for device %s", release.version, device.id)
    return task


def update_status(device: Device, release: AgentRelease | None) -> str:
    """What the dashboard shows for this machine's agent version."""
    if release is None:
        return "unknown"
    if device.agent_version == release.version:
        return "up to date"
    if device.last_update_result == "reverted":
        return "rolled back"
    if not manages_updates(device.agent_version):
        # Shown before "held back": the state that matters for these machines
        # is that they need the installer, not what the fleet version is doing.
        return "not managed"
    if release.quarantined_at is not None:
        return "held back"
    return "update pending"


def clear_caches() -> None:
    _version_for.cache_clear()
