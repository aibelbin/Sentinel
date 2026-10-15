"""The .deb the agent ships as.

The point of the package is `Depends: python3, cron` -- apt resolving the
agent's dependencies instead of a human preparing the machine -- so that is
what these assert hardest on.
"""

import shutil
import subprocess
import tarfile

import pytest

from app.services import agent_release, debian_package
from app.services.installer import clear_caches

pytestmark = pytest.mark.skipif(
    shutil.which("dpkg-deb") is None, reason="dpkg-deb is not installed"
)


@pytest.fixture(autouse=True)
def _caches():
    debian_package.clear_caches()
    agent_release.clear_caches()
    clear_caches()
    yield
    debian_package.clear_caches()
    agent_release.clear_caches()
    clear_caches()


@pytest.fixture
def built(tmp_path):
    """Build the package from the real agent source and unpack it for reading."""
    from app.config import get_settings

    source = get_settings().agent_source_dir
    version, _ = agent_release.current_release()
    data = debian_package.build(source, version)

    path = tmp_path / debian_package.filename(version)
    path.write_bytes(data)
    return version, path


def _field(path, name: str) -> str:
    out = subprocess.run(
        ["dpkg-deb", "--field", str(path), name], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _contents(path) -> list[str]:
    """Paths inside the package.

    dpkg-deb prints a symlink as `./usr/bin/x -> /target`, so the last field is
    the target rather than the member; split the arrow off first.
    """
    out = subprocess.run(
        ["dpkg-deb", "--contents", str(path)], capture_output=True, text=True, check=True
    )
    names = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        member = line.split(" -> ", 1)[0]
        names.append(member.split()[-1].lstrip("."))
    return names


# --------------------------------------------------------------------------- #
# The reason the package exists
# --------------------------------------------------------------------------- #


def test_it_declares_its_dependencies(built):
    """So apt installs python3 and cron, and no machine is prepared by hand."""
    _version, path = built
    depends = _field(path, "Depends")
    assert "python3" in depends
    assert "cron" in depends


def test_it_requires_a_python_the_agent_actually_runs_on(built):
    _version, path = built
    assert ">= 3.8" in _field(path, "Depends")


def test_it_is_architecture_independent(built):
    """The agent is pure Python, so one package serves every machine."""
    _version, path = built
    assert _field(path, "Architecture") == "all"


# --------------------------------------------------------------------------- #
# Contents
# --------------------------------------------------------------------------- #


def test_it_ships_the_launcher_and_the_agent(built):
    version, path = built
    names = _contents(path)
    assert "/opt/sentinel-agent/bin/sentinel-agent" in names
    assert f"/opt/sentinel-agent/live/versions/{version}/sentinel_agent/cli.py" in names
    assert any(name.endswith("/collectors/os_pkgs.py") for name in names)


def test_it_puts_the_agent_on_path(built):
    _version, path = built
    assert "/usr/bin/sentinel-agent" in _contents(path)


def test_it_carries_no_compiled_python(built):
    _version, path = built
    assert not [name for name in _contents(path) if name.endswith(".pyc")]
    assert not [name for name in _contents(path) if "__pycache__" in name]


def test_it_carries_no_credentials(built):
    """The package is identical for every machine, so it cannot contain a key.
    Enrolment is a separate step."""
    _version, path = built
    names = _contents(path)
    assert not [name for name in names if name.endswith("config.json")]

    raw = path.read_bytes()
    assert b"sk_dev_" not in raw


def test_the_version_carries_the_source_hash(built):
    """So that a redeploy is an upgrade apt can reason about."""
    version, path = built
    assert _field(path, "Version") == f"{debian_package.BASE_VERSION}+{version}"
    assert version in _field(path, "Version")


# --------------------------------------------------------------------------- #
# Maintainer scripts
# --------------------------------------------------------------------------- #


def test_it_has_executable_maintainer_scripts(built, tmp_path):
    _version, path = built
    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["dpkg-deb", "--control", str(path), str(control)], check=True)

    for script in ("postinst", "prerm", "postrm"):
        target = control / script
        assert target.is_file(), f"{script} missing"
        assert target.stat().st_mode & 0o111, f"{script} is not executable"
        # They run under dpkg's /bin/sh, so they must be valid POSIX shell.
        subprocess.run(["sh", "-n", str(target)], check=True)


def test_postinst_does_not_try_to_enrol(built, tmp_path):
    """A package cannot carry a device key, so it must not pretend to."""
    _version, path = built
    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["dpkg-deb", "--control", str(path), str(control)], check=True)
    postinst = (control / "postinst").read_text()
    assert "enroll" in postinst  # it tells the operator what to run
    assert "--key" not in postinst.split("echo")[0]  # but never runs it itself


def test_purge_removes_self_installed_versions(built, tmp_path):
    """Versions the agent installed itself are outside dpkg's file list, so
    postrm has to remove the tree explicitly or they are left behind."""
    _version, path = built
    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["dpkg-deb", "--control", str(path), str(control)], check=True)
    postrm = (control / "postrm").read_text()
    assert "/opt/sentinel-agent" in postrm
    assert "purge" in postrm


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


def test_editing_the_agent_rebuilds_the_package(tmp_path, monkeypatch):
    from app.config import get_settings

    source = tmp_path / "agent"
    (source / "sentinel_agent").mkdir(parents=True)
    (source / "bin").mkdir()
    (source / "sentinel_agent" / "cli.py").write_text("def main(argv=None):\n    return 0\n")
    (source / "bin" / "sentinel-agent").write_text("#!/usr/bin/env python3\n")

    first = debian_package.build(str(source), "aaaaaaaaaaaa")
    (source / "sentinel_agent" / "extra.py").write_text("# a new feature\n")
    second = debian_package.build(str(source), "bbbbbbbbbbbb")

    assert first != second
    assert debian_package.deb_version("bbbbbbbbbbbb").endswith("bbbbbbbbbbbb")
