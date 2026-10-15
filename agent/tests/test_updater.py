"""Installing a new version: what gets rejected, what gets swapped, and what
happens when a release will not run."""

import base64
import gzip
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest

from sentinel_agent import paths, state, updater
from sentinel_agent.config import Config

CONFIG = Config(server_url="http://api:8000", api_key="sk_dev_test", insecure=True)

GOOD_CLI = """
def main(argv=None):
    return 0
"""

BROKEN_CLI = """
raise RuntimeError("this release does not import")
"""

FAILING_SELFTEST = """
import sys
def main(argv=None):
    if argv and argv[0] == "selftest":
        print("deliberately broken", file=sys.stderr)
        return 1
    return 0
"""


def make_bundle(cli_source: str = GOOD_CLI) -> bytes:
    """A tar.gz shaped like a real agent bundle."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name, body in (
                ("sentinel_agent/__init__.py", "__version__ = 'test'\n"),
                ("sentinel_agent/cli.py", cli_source),
            ):
                data = body.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def served(raw: bytes, version: str, *, sha256: str | None = None) -> dict:
    return {
        "version": version,
        "sha256": sha256 if sha256 is not None else hashlib.sha256(raw).hexdigest(),
        "bundle_b64": base64.b64encode(raw).decode(),
    }


@pytest.fixture
def install_root(sandbox, monkeypatch):
    """A versioned install laid out the way the installer leaves it."""
    root = sandbox / "opt"
    (root / "versions" / "v1" / "sentinel_agent").mkdir(parents=True)
    (root / "versions" / "v1" / "sentinel_agent" / "cli.py").write_text(GOOD_CLI)
    (root / "bin").mkdir()
    os.symlink(root / "versions" / "v1", root / "current")

    monkeypatch.setenv("SENTINEL_INSTALL_ROOT", str(root))
    import importlib

    importlib.reload(paths)
    importlib.reload(state)
    importlib.reload(updater)
    monkeypatch.setattr(updater, "running_version", lambda: "v1")
    return root


def serve(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(updater, "get", lambda _cfg, _path: payload)


# --------------------------------------------------------------------------- #
# What gets rejected before anything is written
# --------------------------------------------------------------------------- #


def test_checksum_mismatch_is_refused(install_root, monkeypatch):
    """A truncated or corrupted download must never be installed."""
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2", sha256="0" * 64))

    with pytest.raises(updater.UpdateFailed, match="checksum mismatch"):
        updater.install(CONFIG, "v2", "0" * 64)

    assert not (install_root / "versions" / "v2").exists()
    assert (install_root / "current").resolve().name == "v1"


def test_a_version_the_server_did_not_serve_is_refused(install_root, monkeypatch):
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v3"))
    with pytest.raises(updater.UpdateFailed, match="served"):
        updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())


def test_an_empty_bundle_is_refused(install_root, monkeypatch):
    serve(monkeypatch, served(b"", "v2"))
    with pytest.raises(updater.UpdateFailed, match="empty"):
        updater.install(CONFIG, "v2", hashlib.sha256(b"").hexdigest())


def test_a_bundle_without_the_agent_is_refused(install_root, monkeypatch):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            data = b"not an agent"
            info = tarfile.TarInfo("README")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    serve(monkeypatch, served(raw, "v2"))

    with pytest.raises(updater.UpdateFailed, match="cli.py"):
        updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())


def test_a_path_traversing_bundle_is_refused(install_root, monkeypatch):
    """A bundle must not be able to write outside the version directory."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            data = b"owned"
            info = tarfile.TarInfo("../../escaped.py")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    serve(monkeypatch, served(raw, "v2"))

    with pytest.raises(updater.UpdateFailed, match="escapes"):
        updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())
    assert not (install_root.parent / "escaped.py").exists()


def test_installing_the_running_version_is_a_no_op(install_root, monkeypatch):
    serve(monkeypatch, served(make_bundle(), "v1"))
    outcome = updater.install(CONFIG, "v1", "0" * 64)
    assert outcome["installed"] is False


# --------------------------------------------------------------------------- #
# The self-test gate
# --------------------------------------------------------------------------- #


def test_a_release_that_fails_its_selftest_is_not_swapped_in(install_root, monkeypatch):
    """This is what stops a broken release reaching the fleet."""
    raw = make_bundle(FAILING_SELFTEST)
    serve(monkeypatch, served(raw, "v2"))

    with pytest.raises(updater.UpdateFailed, match="self-test failed"):
        updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())

    assert (install_root / "current").resolve().name == "v1"
    assert not (install_root / "versions" / "v2").exists()
    assert not paths.PENDING_VERSION_PATH.exists()


def test_a_release_that_cannot_import_is_not_swapped_in(install_root, monkeypatch):
    raw = make_bundle(BROKEN_CLI)
    serve(monkeypatch, served(raw, "v2"))

    with pytest.raises(updater.UpdateFailed, match="self-test"):
        updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())
    assert (install_root / "current").resolve().name == "v1"


# --------------------------------------------------------------------------- #
# A successful install
# --------------------------------------------------------------------------- #


def test_a_good_release_is_installed_and_becomes_current(install_root, monkeypatch):
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2"))

    outcome = updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())

    assert outcome["installed"] is True
    assert outcome["previous_version"] == "v1"
    assert (install_root / "current").resolve().name == "v2"
    assert (install_root / "versions" / "v2" / "sentinel_agent" / "cli.py").is_file()


def test_the_previous_version_stays_on_disk(install_root, monkeypatch):
    """There must always be something known-good to fall back to."""
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2"))
    updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())
    assert (install_root / "versions" / "v1").is_dir()


def test_a_marker_puts_the_new_version_on_probation(install_root, monkeypatch):
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2"))
    updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())

    marker = json.loads(paths.PENDING_VERSION_PATH.read_text())
    assert marker["version"] == "v2"
    assert marker["fallback"] == "v1"
    assert marker["swapped_at"] > 0


def test_completing_a_beat_takes_a_version_off_probation(install_root, monkeypatch):
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2"))
    updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())

    monkeypatch.setattr(updater, "running_version", lambda: "v2")
    assert updater.confirm_running_version() is True
    assert not paths.PENDING_VERSION_PATH.exists()
    assert updater.confirmed_version() == "v2"
    # Idempotent: every later beat must not re-announce it.
    assert updater.confirm_running_version() is False


# --------------------------------------------------------------------------- #
# Retention: keep the last proven version until its successor proves itself
# --------------------------------------------------------------------------- #


def test_shipping_v2_keeps_v1(install_root, monkeypatch):
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2"))
    updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())
    assert {p.name for p in (install_root / "versions").iterdir()} == {"v1", "v2"}


def test_shipping_v3_drops_v1_once_v2_proved_itself(install_root, monkeypatch):
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2"))
    updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())

    monkeypatch.setattr(updater, "running_version", lambda: "v2")
    updater.confirm_running_version()

    serve(monkeypatch, served(raw, "v3"))
    updater.install(CONFIG, "v3", hashlib.sha256(raw).hexdigest())

    remaining = {p.name for p in (install_root / "versions").iterdir()}
    assert remaining == {"v2", "v3"}


def test_an_unproven_version_is_not_used_as_the_fallback(install_root, monkeypatch):
    """If v2 never completed a beat, falling back to it would be no safer than
    going forward, so v1 stays the fallback."""
    raw = make_bundle()
    serve(monkeypatch, served(raw, "v2"))
    updater.install(CONFIG, "v2", hashlib.sha256(raw).hexdigest())

    # v2 is current but never confirmed.
    monkeypatch.setattr(updater, "running_version", lambda: "v2")
    serve(monkeypatch, served(raw, "v3"))
    updater.install(CONFIG, "v3", hashlib.sha256(raw).hexdigest())

    marker = json.loads(paths.PENDING_VERSION_PATH.read_text())
    assert marker["fallback"] == "v1"
    assert (install_root / "versions" / "v1").is_dir()


# --------------------------------------------------------------------------- #
# The launcher's fallback
# --------------------------------------------------------------------------- #


def _run_launcher(sandbox, install_root, extra_env=None):
    import subprocess
    import sys

    launcher = Path(__file__).resolve().parents[1] / "bin" / "sentinel-agent"
    env = {
        "PATH": "/usr/bin:/bin",
        "SENTINEL_INSTALL_ROOT": str(install_root),
        "SENTINEL_STATE_DIR": str(sandbox / "state"),
        "SENTINEL_LOG_DIR": str(sandbox / "log"),
        "SENTINEL_CONFIG": str(sandbox / "etc" / "config.json"),
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(launcher), "status"],
        capture_output=True, text=True, env=env, check=False,
    )


def test_launcher_reverts_a_version_that_never_completed_a_beat(sandbox, install_root):
    """The case that would otherwise leave a machine dark with nothing running
    to repair it."""
    (install_root / "versions" / "v2" / "sentinel_agent").mkdir(parents=True)
    (install_root / "versions" / "v2" / "sentinel_agent" / "cli.py").write_text(BROKEN_CLI)
    (install_root / "current").unlink()
    os.symlink(install_root / "versions" / "v2", install_root / "current")

    marker = {"version": "v2", "fallback": "v1", "swapped_at": int(time.time()) - 600}
    paths.PENDING_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.PENDING_VERSION_PATH.write_text(json.dumps(marker))

    result = _run_launcher(sandbox, install_root)

    assert "reverted to v1" in result.stderr
    assert (install_root / "current").resolve().name == "v1"
    assert not paths.PENDING_VERSION_PATH.exists()


def test_launcher_leaves_a_version_alone_inside_its_grace_period(sandbox, install_root):
    """A single slow run must not be mistaken for a broken release."""
    marker = {"version": "v1", "fallback": "v1", "swapped_at": int(time.time())}
    paths.PENDING_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.PENDING_VERSION_PATH.write_text(json.dumps(marker))

    _run_launcher(sandbox, install_root)

    assert paths.PENDING_VERSION_PATH.exists()
    assert (install_root / "current").resolve().name == "v1"


def _stub_that_records(path: Path, label: str) -> str:
    return (
        "def main(argv=None):\n"
        f"    open({str(path)!r}, 'w').write({label!r})\n"
        "    return 0\n"
    )


def test_launcher_runs_the_code_current_points_at(sandbox, install_root):
    """The launcher's whole job: execute whatever `current` resolves to."""
    receipt = install_root / "ran.txt"
    (install_root / "versions" / "v1" / "sentinel_agent" / "cli.py").write_text(
        _stub_that_records(receipt, "v1")
    )

    result = _run_launcher(sandbox, install_root)

    assert result.returncode == 0, result.stderr
    assert receipt.read_text() == "v1"


def test_launcher_follows_current_to_a_new_version(sandbox, install_root):
    """After an update swaps the symlink, the next run must execute the new
    code without the launcher itself changing."""
    receipt = install_root / "ran.txt"
    (install_root / "versions" / "v1" / "sentinel_agent" / "cli.py").write_text(
        _stub_that_records(receipt, "v1")
    )
    (install_root / "versions" / "v2" / "sentinel_agent").mkdir(parents=True)
    (install_root / "versions" / "v2" / "sentinel_agent" / "cli.py").write_text(
        _stub_that_records(receipt, "v2")
    )

    _run_launcher(sandbox, install_root)
    assert receipt.read_text() == "v1"

    (install_root / "current").unlink()
    os.symlink(install_root / "versions" / "v2", install_root / "current")

    _run_launcher(sandbox, install_root)
    assert receipt.read_text() == "v2"
