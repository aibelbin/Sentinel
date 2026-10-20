"""Builds the .deb the watchdog ships as.

This exists for one reason: `Depends: python3, cron` means apt resolves the
watchdog's dependencies, so no machine has to be prepared by hand before it can
be monitored. Upgrade, clean uninstall and file ownership come free with it.

The package is built on demand from the same watchdog source the installer script
embeds, and cached on a fingerprint of that source so an edit invalidates it
without a restart.
"""

import logging
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from app.services.installer import BUNDLE_MEMBERS, LAUNCHER_MEMBERS, source_fingerprint

log = logging.getLogger(__name__)

PACKAGE_NAME = "sentinel-watchdog"
# The upstream release, which the source hash is appended to so that every
# deployed build has a distinct, ordered package version.
BASE_VERSION = "1.0.0"

MAINTAINER_SCRIPTS = ("postinst", "prerm", "postrm")


class PackageBuildFailed(Exception):
    pass


def deb_version(watchdog_version: str) -> str:
    """A Debian-legal version carrying the source hash.

    `+` sorts after nothing, so 1.0.0+abc is newer than 1.0.0 and two builds
    compare consistently -- which is what lets apt treat a redeploy as an
    upgrade.
    """
    return f"{BASE_VERSION}+{watchdog_version}"


def filename(watchdog_version: str) -> str:
    return f"{PACKAGE_NAME}_{deb_version(watchdog_version)}_all.deb"


def _packaging_dir() -> Path:
    # packaging/ sits beside server/ and watchdog/ in the repo, and is copied
    # into the image alongside them.
    for candidate in (Path("/app/packaging"), Path(__file__).resolve().parents[3] / "packaging"):
        if (candidate / "debian" / "control").is_file():
            return candidate
    raise PackageBuildFailed("cannot find the packaging/debian directory")


def _stage(root: Path, watchdog_source: Path, watchdog_version: str) -> None:
    """Lay out the file tree exactly as it will land on the target machine."""
    install_dir = root / "opt" / "sentinel-watchdog"
    version_dir = install_dir / "live" / "versions" / watchdog_version
    version_dir.mkdir(parents=True)
    (install_dir / "bin").mkdir(parents=True, exist_ok=True)

    for name in BUNDLE_MEMBERS:
        shutil.copytree(
            watchdog_source / name,
            version_dir / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for name in LAUNCHER_MEMBERS:
        for item in sorted((watchdog_source / name).iterdir()):
            if item.is_file():
                shutil.copy2(item, install_dir / "bin" / item.name)

    # postinst reads this to decide which version `current` should point at.
    (install_dir / ".packaged-version").write_text(watchdog_version, encoding="utf-8")
    (install_dir / "live" / "current").symlink_to(f"versions/{watchdog_version}")

    # On PATH, so `sentinel-watchdog enroll` works straight after apt install.
    usr_bin = root / "usr" / "bin"
    usr_bin.mkdir(parents=True)
    (usr_bin / "sentinel-watchdog").symlink_to("/opt/sentinel-watchdog/bin/sentinel-watchdog")

    control_dir = root / "DEBIAN"
    control_dir.mkdir()
    packaging = _packaging_dir() / "debian"
    control = (packaging / "control").read_text(encoding="utf-8")
    if "@@DEB_VERSION@@" not in control:
        raise PackageBuildFailed("control file is missing the @@DEB_VERSION@@ placeholder")
    (control_dir / "control").write_text(
        control.replace("@@DEB_VERSION@@", deb_version(watchdog_version)), encoding="utf-8"
    )
    for script in MAINTAINER_SCRIPTS:
        target = control_dir / script
        shutil.copy2(packaging / script, target)
        target.chmod(0o755)


def _set_modes(root: Path, watchdog_version: str) -> None:
    install_dir = root / "opt" / "sentinel-watchdog"
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o755 if path.is_dir() else 0o644)
    (install_dir / "bin" / "sentinel-watchdog").chmod(0o755)
    for script in MAINTAINER_SCRIPTS:
        (root / "DEBIAN" / script).chmod(0o755)


def _packaging_fingerprint() -> tuple:
    """Newest mtime and total size across the maintainer scripts.

    Part of the build cache key: the scripts are not watchdog source, so editing
    one does not change the watchdog version, and without this a running server
    would keep serving a .deb built from the old postinst.
    """
    newest = 0.0
    total = 0
    packaging = _packaging_dir() / "debian"
    for path in sorted(packaging.iterdir()):
        if not path.is_file():
            continue
        stat = path.stat()
        newest = max(newest, stat.st_mtime)
        total += stat.st_size
    return (newest, total)


@lru_cache(maxsize=4)
def _build_cached(
    watchdog_source_dir: str, watchdog_version: str, _fingerprint: tuple, _packaging: tuple
) -> bytes:
    watchdog_source = Path(watchdog_source_dir)
    if shutil.which("dpkg-deb") is None:
        raise PackageBuildFailed(
            "dpkg-deb is not installed in this image, so the .deb cannot be built"
        )

    with tempfile.TemporaryDirectory(prefix="sentinel-deb-") as tmp:
        root = Path(tmp) / "pkg"
        root.mkdir()
        _stage(root, watchdog_source, watchdog_version)
        _set_modes(root, watchdog_version)

        output = Path(tmp) / filename(watchdog_version)
        completed = subprocess.run(  # noqa: S603 - argv list, shell=False
            # --root-owner-group so the package does not carry the build
            # user's uid, which would otherwise end up owning files on every
            # machine that installs it.
            ["dpkg-deb", "--root-owner-group", "--build", str(root), str(output)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise PackageBuildFailed(
                f"dpkg-deb failed: {detail[-1][:300] if detail else completed.returncode}"
            )

        data = output.read_bytes()

    log.info("built %s (%d bytes)", filename(watchdog_version), len(data))
    return data


def build(watchdog_source_dir: str, watchdog_version: str) -> bytes:
    return _build_cached(
        watchdog_source_dir,
        watchdog_version,
        source_fingerprint(Path(watchdog_source_dir)),
        _packaging_fingerprint(),
    )


def clear_caches() -> None:
    _build_cached.cache_clear()
