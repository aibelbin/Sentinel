"""Resource sampling from /proc. No external commands, no third-party packages."""

import logging
import os

from sentinel_watchdog import paths, state

log = logging.getLogger("sentinel.resources")

CPU_SAMPLE_KEY = "cpu_sample"

# Pseudo and overlay filesystems carry no real capacity, and reporting them
# produces meaningless 100%-full rows on the dashboard.
REAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "vfat", "f2fs", "jfs", "reiserfs"}


def _read_text(path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("cannot read %s: %s", path, exc)
        return None


def _cpu_totals() -> tuple[int, int] | None:
    """Return (idle_jiffies, total_jiffies) from the aggregate /proc/stat line."""
    text = _read_text(paths.PROC_DIR / "stat")
    if not text:
        return None
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = [int(value) for value in line.split()[1:]]
        if len(fields) < 5:
            return None
        # user nice system idle iowait irq softirq steal guest guest_nice
        idle = fields[3] + fields[4]
        return idle, sum(fields)
    return None


def cpu_percent() -> float | None:
    """Busy percentage since the previous beat.

    Returns None on the first beat after enrollment: a percentage needs two
    samples to difference, and there is no prior one yet. Reporting 0 would be
    a lie, so the field is left empty and the dashboard shows it as unknown.
    """
    current = _cpu_totals()
    if current is None:
        return None

    previous = state.get(CPU_SAMPLE_KEY)
    state.set(CPU_SAMPLE_KEY, list(current))

    if not previous or len(previous) != 2:
        return None

    idle_delta = current[0] - previous[0]
    total_delta = current[1] - previous[1]
    if total_delta <= 0:
        # Counters reset, which means the host rebooted between beats.
        return None

    busy = 100.0 * (1.0 - idle_delta / total_delta)
    return round(max(0.0, min(100.0, busy)), 2)


def memory() -> tuple[int | None, int | None]:
    text = _read_text(paths.PROC_DIR / "meminfo")
    if not text:
        return None, None

    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024  # /proc/meminfo reports kB

    return values.get("MemTotal"), values.get("MemAvailable")


def load_average() -> tuple[float | None, float | None, float | None]:
    text = _read_text(paths.PROC_DIR / "loadavg")
    if not text:
        return None, None, None
    parts = text.split()
    if len(parts) < 3:
        return None, None, None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None, None, None


def uptime_seconds() -> int | None:
    text = _read_text(paths.PROC_DIR / "uptime")
    if not text:
        return None
    try:
        return int(float(text.split()[0]))
    except (ValueError, IndexError):
        return None


def disks() -> list[dict]:
    """Capacity per real mounted filesystem, one entry per backing device."""
    text = _read_text(paths.PROC_DIR / "mounts")
    if not text:
        return []

    # Keep the shallowest mountpoint for each backing device. A device can be
    # mounted many times -- bind mounts, and in containers single *files* like
    # /etc/hostname -- and taking whichever appeared first in /proc/mounts can
    # report a bind mount in place of the real root filesystem.
    best: dict[str, str] = {}
    filesystems: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, filesystem = fields[0], fields[1], fields[2]

        mountpoint = mountpoint.replace("\\040", " ")
        # The root filesystem is always reported, whatever it sits on: overlay
        # in a container, zfs, f2fs, or something not in the list below.
        # Excluding it would hide the one filesystem that always matters, and
        # an allowlist cannot be trusted to be complete.
        if mountpoint != "/" and filesystem not in REAL_FILESYSTEMS:
            continue

        # A mount of a single file carries no capacity of its own.
        if not os.path.isdir(mountpoint):
            continue

        current = best.get(device)
        if current is None or len(mountpoint) < len(current):
            best[device] = mountpoint
            filesystems[device] = filesystem

    out: list[dict] = []
    for device, mountpoint in sorted(best.items(), key=lambda item: item[1]):
        try:
            stat = os.statvfs(mountpoint)
        except OSError as exc:
            log.debug("cannot statvfs %s: %s", mountpoint, exc)
            continue
        out.append(
            {
                "mountpoint": mountpoint[:255],
                "filesystem": filesystems[device][:64],
                "total_bytes": stat.f_blocks * stat.f_frsize,
                # f_bavail, not f_bfree: space actually usable by non-root.
                "free_bytes": stat.f_bavail * stat.f_frsize,
            }
        )
        if len(out) >= 64:
            break
    return out


def sample() -> dict:
    """One full resource sample for the heartbeat body."""
    total, available = memory()
    load_1, load_5, load_15 = load_average()
    return {
        "cpu_percent": cpu_percent(),
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "load_1": load_1,
        "load_5": load_5,
        "load_15": load_15,
        "uptime_seconds": uptime_seconds(),
        "disks": disks(),
    }

