"""Installing a new watchdog version.

The server decides which version a machine should run and hands it over as an
`update_watchdog` job. This module does the work: fetch, check, prove the new code
can start, then swap a symlink. It never tries to become the new version
in-process — it reports success and exits, and the next cron tick runs the new
code.

Nothing here runs with elevated privileges; the install tree belongs to the
watchdog's own user.
"""

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from sentinel_watchdog import paths, state
from sentinel_watchdog.config import Config
from sentinel_watchdog.http import get
from sentinel_watchdog.version import running_version

log = logging.getLogger("sentinel.updater")

CONFIRMED_KEY = "confirmed_version"

# A bundle is the watchdog source, so tens of kilobytes. The cap is a sanity
# bound against a wrong or hostile response, not a tuning parameter.
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
SELFTEST_TIMEOUT = 60


class UpdateFailed(Exception):
    """The update did not happen and the running version is untouched."""


# --------------------------------------------------------------------------- #
# Fetch and verify
# --------------------------------------------------------------------------- #


def _fetch(cfg: Config, version: str) -> bytes:
    payload = get(cfg, f"/api/v1/watchdog/bundle?version={version}")

    served = payload.get("version")
    if served != version:
        raise UpdateFailed(f"asked for {version} but the server served {served}")

    encoded = payload.get("bundle_b64") or ""
    expected = (payload.get("sha256") or "").lower()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateFailed(f"bundle was not valid base64: {exc}") from exc

    if not raw:
        raise UpdateFailed("server returned an empty bundle")
    if len(raw) > MAX_BUNDLE_BYTES:
        raise UpdateFailed(f"bundle is {len(raw)} bytes, over the {MAX_BUNDLE_BYTES} cap")

    actual = hashlib.sha256(raw).hexdigest()
    if not expected:
        raise UpdateFailed("server did not supply a checksum")
    if actual != expected:
        # A truncated or corrupted transfer, which is the failure this guards
        # against. Installing it would brick the machine for no reason.
        raise UpdateFailed(f"checksum mismatch: expected {expected}, got {actual}")

    return raw


def _safe_extract(raw: bytes, destination: Path) -> None:
    """Unpack the bundle, refusing any member that would escape the target."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                raise UpdateFailed(f"bundle contains a non-regular entry: {member.name}")
            resolved = (destination / member.name).resolve()
            if not str(resolved).startswith(str(destination.resolve()) + os.sep):
                raise UpdateFailed(f"bundle entry escapes the target: {member.name}")
        try:
            # filter="data" is the safe default from Python 3.12; Ubuntu 20.04
            # ships 3.8, where the argument does not exist. Members are already
            # validated above either way.
            tar.extractall(destination, filter="data")  # noqa: S202
        except TypeError:
            tar.extractall(destination)  # noqa: S202 - members validated above

    if not (destination / "sentinel_watchdog" / "cli.py").is_file():
        raise UpdateFailed("bundle does not contain sentinel_watchdog/cli.py")


# --------------------------------------------------------------------------- #
# Prove it can run
# --------------------------------------------------------------------------- #


def _selftest(version_dir: Path) -> None:
    """Run the new code's self-test in a separate process.

    This is what stops a broken release from ever being swapped in. Running it
    out-of-process means an import error or a hard exit is caught rather than
    taking down the watchdog doing the install.
    """
    script = (
        "import sys; sys.path.insert(0, %r); "
        "from sentinel_watchdog.cli import main; "
        "raise SystemExit(main(['selftest']))" % str(version_dir)
    )
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, shell=False
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=SELFTEST_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateFailed(f"self-test timed out after {SELFTEST_TIMEOUT}s") from exc
    except OSError as exc:
        raise UpdateFailed(f"could not run the self-test: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        hint = detail[-1][:300] if detail else f"exit {completed.returncode}"
        raise UpdateFailed(f"self-test failed: {hint}")


# --------------------------------------------------------------------------- #
# Swap, confirm, prune
# --------------------------------------------------------------------------- #


def _point_current_at(version: str) -> None:
    target = paths.VERSIONS_DIR / version
    staging = paths.LIVE_DIR / ".current.swap"
    if staging.is_symlink() or staging.exists():
        staging.unlink()
    staging.symlink_to(target)
    # Atomic: there is no moment where `current` fails to resolve.
    os.replace(staging, paths.CURRENT_LINK)


def confirmed_version() -> str | None:
    value = state.get(CONFIRMED_KEY)
    return value if isinstance(value, str) else None


def confirm_running_version() -> bool:
    """Mark the running version as proven. Called after a beat succeeds.

    Returns True if this call is what cleared probation, so the caller can log
    it once rather than on every beat.
    """
    running = running_version()
    marker = _read_marker()

    newly_confirmed = False
    if marker is not None and marker.get("version") == running:
        try:
            paths.PENDING_VERSION_PATH.unlink()
        except OSError:
            pass
        newly_confirmed = True

    if confirmed_version() != running:
        state.set(CONFIRMED_KEY, running)

    return newly_confirmed


def _read_marker() -> dict | None:
    try:
        marker = json.loads(paths.PENDING_VERSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def _write_marker(version: str, fallback: str) -> None:
    paths.PENDING_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": version, "fallback": fallback, "swapped_at": int(time.time())}
    tmp = paths.PENDING_VERSION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, paths.PENDING_VERSION_PATH)


def prune(keep: set[str]) -> list[str]:
    """Delete version directories outside `keep`.

    Called at install time, never at confirm time: the previous version has to
    survive until its successor has proven itself, so that there is always
    something known-good to fall back to.
    """
    if not paths.VERSIONS_DIR.is_dir():
        return []

    removed = []
    for entry in sorted(paths.VERSIONS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in keep:
            continue
        try:
            shutil.rmtree(entry)
            removed.append(entry.name)
        except OSError as exc:
            log.warning("could not remove old version %s: %s", entry.name, exc)
    return removed


# --------------------------------------------------------------------------- #
# The job handler's entry point
# --------------------------------------------------------------------------- #


def install(cfg: Config, version: str, sha256: str) -> dict:
    """Install `version` and point `current` at it."""
    running = running_version()
    if version == running:
        return {"installed": False, "reason": "already running this version",
                "version": running}

    if not paths.LIVE_DIR.is_dir():
        raise UpdateFailed(
            f"{paths.LIVE_DIR} does not exist; this watchdog was not installed "
            "as a versioned layout and cannot update itself"
        )

    log.info("installing version %s (running %s)", version, running)
    raw = _fetch(cfg, version)

    paths.VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=paths.VERSIONS_DIR))
    final = paths.VERSIONS_DIR / version
    try:
        _safe_extract(raw, staging)
        _selftest(staging)

        if final.is_dir():
            shutil.rmtree(final)
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # The fallback has to be a version known to work. In order of preference:
    # the one this machine is already holding as its fallback (set when the
    # running version is itself on probation), then the last proven version,
    # and only then the running one. Without the first case, installing over an
    # unproven version would adopt it as the fallback and lose the last good
    # one.
    marker = _read_marker()
    if marker is not None and marker.get("version") == running and marker.get("fallback"):
        fallback = marker["fallback"]
    else:
        fallback = confirmed_version() or running
    _write_marker(version, fallback)
    try:
        _point_current_at(version)
    except OSError as exc:
        paths.PENDING_VERSION_PATH.unlink(missing_ok=True)
        raise UpdateFailed(f"could not switch to {version}: {exc}") from exc

    # running is always kept: when it is itself unproven the fallback is an
    # older version, and without this the directory this very process is
    # executing from would be deleted mid-beat.
    removed = prune({version, fallback, running})
    log.info("installed %s, fallback %s, removed %s", version, fallback, removed or "nothing")

    return {
        "installed": True,
        "version": version,
        "previous_version": running,
        "fallback": fallback,
        "removed": removed,
    }
