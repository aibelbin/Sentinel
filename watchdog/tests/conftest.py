"""Watchdog test fixtures.

Every path the watchdog touches is redirected into a temporary directory, so tests
never read or write a real install.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point config, state and logs at tmp_path for the duration of a test."""
    monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "etc" / "config.json"))
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SENTINEL_LOG_DIR", str(tmp_path / "log"))

    # paths reads the environment at import time, so it has to be reloaded
    # after the variables change.
    from sentinel_watchdog import paths

    importlib.reload(paths)
    for name in ("config", "state", "spool", "resources", "version", "updater"):
        module = importlib.import_module(f"sentinel_watchdog.{name}")
        importlib.reload(module)

    return tmp_path


@pytest.fixture
def proc_dir(tmp_path, monkeypatch):
    """A fake /proc, so resource parsing is tested against known content."""
    directory = tmp_path / "proc"
    directory.mkdir()
    monkeypatch.setenv("SENTINEL_PROC_DIR", str(directory))

    import importlib

    from sentinel_watchdog import paths

    importlib.reload(paths)
    from sentinel_watchdog import resources

    importlib.reload(resources)
    return directory


@pytest.fixture
def stub_bin(tmp_path, monkeypatch):
    """Put a directory of fake executables first on PATH.

    This runs each collector's real code path against known command output,
    rather than mocking the collector itself.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")

    def add(name: str, stdout: str = "", stderr: str = "", exit_code: int = 0):
        script = directory / name
        script.write_text(
            "#!/bin/sh\n"
            f"cat <<'STDOUT_EOF'\n{stdout}\nSTDOUT_EOF\n"
            f"cat >&2 <<'STDERR_EOF'\n{stderr}\nSTDERR_EOF\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    return add
