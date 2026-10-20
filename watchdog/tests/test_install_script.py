"""Shell-level tests for the cron detection the installer and the package share.

A dead cron inside a container does not disappear: the container's PID 1 is the
application, which never reaps its children, so the exited process stays in
/proc as a zombie. Detection that only reads process names -- which both pgrep
and a naive /proc scan do -- then reports cron as running and nothing restarts
it, leaving the device silently unmonitored.
"""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCES = {
    "install.sh": REPO / "watchdog" / "install.sh",
    "postinst": REPO / "packaging" / "debian" / "postinst",
}


def _extract(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("cron_is_running() {"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _fake_proc(root: Path, entries: dict[int, tuple[str, str]]) -> Path:
    """Build a /proc-shaped tree. entries maps pid -> (command name, state)."""
    proc = root / "proc"
    proc.mkdir()
    for pid, (name, state) in entries.items():
        entry = proc / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(f"{name}\n", encoding="utf-8")
        # Field order matches the kernel's: pid, (comm), state, ppid, ...
        (entry / "stat").write_text(f"{pid} ({name}) {state} 1 {pid} 0 -1\n", encoding="utf-8")
    return proc


def _run(source: Path, tmp_path: Path, entries: dict[int, tuple[str, str]]) -> bool:
    proc = _fake_proc(tmp_path, entries)
    # The function reads /proc directly; point it at the fixture instead.
    body = _extract(source).replace("/proc/", f"{proc}/")
    completed = subprocess.run(
        ["/bin/sh", "-c", f"{body}\ncron_is_running"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode in (0, 1), completed.stderr
    return completed.returncode == 0


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_running_cron_is_detected(name, tmp_path):
    assert _run(SOURCES[name], tmp_path, {1: ("sleep", "S"), 42: ("cron", "S")}) is True


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_crond_is_detected(name, tmp_path):
    assert _run(SOURCES[name], tmp_path, {7: ("crond", "S")}) is True


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_zombie_cron_does_not_count_as_running(name, tmp_path):
    """The regression: a killed cron whose entry lingers must read as absent."""
    assert _run(SOURCES[name], tmp_path, {1: ("sleep", "S"), 42: ("cron", "Z")}) is False


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_zombie_beside_a_live_cron_still_counts(name, tmp_path):
    entries = {1: ("sleep", "S"), 42: ("cron", "Z"), 99: ("cron", "S")}
    assert _run(SOURCES[name], tmp_path, entries) is True


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_cron_at_all(name, tmp_path):
    assert _run(SOURCES[name], tmp_path, {1: ("sleep", "S"), 8: ("java", "S")}) is False
