"""Resource sampling against a fake /proc."""

STAT_FIRST = "cpu  100 0 50 850 0 0 0 0 0 0\ncpu0 100 0 50 850 0 0 0 0 0 0\n"
# 100 more jiffies busy, 100 more idle: 50% busy over the interval.
STAT_SECOND = "cpu  200 0 50 950 0 0 0 0 0 0\ncpu0 200 0 50 950 0 0 0 0 0 0\n"

MEMINFO = """MemTotal:        8011164 kB
MemFree:         1020304 kB
MemAvailable:    4023528 kB
Buffers:          153604 kB
"""


def test_first_sample_returns_unknown_rather_than_zero(proc_dir):
    """There is no prior sample to difference against on the first beat.
    Reporting 0% would be a lie."""
    from sentinel_agent import resources

    (proc_dir / "stat").write_text(STAT_FIRST, encoding="utf-8")
    assert resources.cpu_percent() is None


def test_second_sample_differences_against_the_first(proc_dir):
    from sentinel_agent import resources

    (proc_dir / "stat").write_text(STAT_FIRST, encoding="utf-8")
    resources.cpu_percent()

    (proc_dir / "stat").write_text(STAT_SECOND, encoding="utf-8")
    assert resources.cpu_percent() == 50.0


def test_counter_reset_is_reported_as_unknown(proc_dir):
    """Counters going backwards means the host rebooted between beats."""
    from sentinel_agent import resources

    (proc_dir / "stat").write_text(STAT_SECOND, encoding="utf-8")
    resources.cpu_percent()
    (proc_dir / "stat").write_text(STAT_FIRST, encoding="utf-8")
    assert resources.cpu_percent() is None


def test_missing_stat_file_is_unknown(proc_dir):
    from sentinel_agent import resources

    assert resources.cpu_percent() is None


def test_memory_is_converted_from_kb_to_bytes(proc_dir):
    from sentinel_agent import resources

    (proc_dir / "meminfo").write_text(MEMINFO, encoding="utf-8")
    total, available = resources.memory()
    assert total == 8011164 * 1024
    assert available == 4023528 * 1024


def test_memory_missing_file_is_none(proc_dir):
    from sentinel_agent import resources

    assert resources.memory() == (None, None)


def test_load_average_is_parsed(proc_dir):
    from sentinel_agent import resources

    (proc_dir / "loadavg").write_text("0.15 0.22 0.18 1/234 5678\n", encoding="utf-8")
    assert resources.load_average() == (0.15, 0.22, 0.18)


def test_malformed_loadavg_is_none(proc_dir):
    from sentinel_agent import resources

    (proc_dir / "loadavg").write_text("garbage\n", encoding="utf-8")
    assert resources.load_average() == (None, None, None)


def test_uptime_is_truncated_to_seconds(proc_dir):
    from sentinel_agent import resources

    (proc_dir / "uptime").write_text("98234.56 390123.44\n", encoding="utf-8")
    assert resources.uptime_seconds() == 98234


def test_pseudo_filesystems_are_excluded(proc_dir):
    """tmpfs and overlay carry no real capacity; reporting them produces
    meaningless rows."""
    from sentinel_agent import resources

    (proc_dir / "mounts").write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "tmpfs /run tmpfs rw,nosuid 0 0\n"
        "overlay /var/lib/docker/overlay2/x overlay rw 0 0\n"
        "devtmpfs /dev devtmpfs rw 0 0\n"
        "proc /proc proc rw 0 0\n",
        encoding="utf-8",
    )
    mountpoints = {disk["mountpoint"] for disk in resources.disks()}
    assert mountpoints == {"/"}


def test_root_filesystem_is_always_reported(proc_dir):
    """An allowlist cannot be trusted to cover every root filesystem -- overlay
    in a container, zfs, f2fs. The root mount is the one that always matters,
    so it is reported whatever it sits on."""
    from sentinel_agent import resources

    (proc_dir / "mounts").write_text(
        "overlay / overlay rw,relatime 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n",
        encoding="utf-8",
    )
    found = resources.disks()
    assert [disk["mountpoint"] for disk in found] == ["/"]
    assert found[0]["filesystem"] == "overlay"
    assert found[0]["total_bytes"] > 0


def test_non_root_pseudo_filesystem_is_still_excluded(proc_dir):
    """The root exception must not become a licence to report tmpfs anywhere."""
    from sentinel_agent import resources

    (proc_dir / "mounts").write_text(
        "overlay /var/lib/docker/overlay2/abc overlay rw 0 0\n"
        "tmpfs /dev/shm tmpfs rw 0 0\n",
        encoding="utf-8",
    )
    assert resources.disks() == []


def test_bind_mount_keeps_the_shallowest_mountpoint(proc_dir, tmp_path):
    """One device mounted twice must be counted once -- and the entry kept has
    to be the real filesystem root, not a deeper bind mount of it."""
    from sentinel_agent import resources

    root = tmp_path / "fsroot"
    deep = root / "nested" / "bind"
    deep.mkdir(parents=True)

    (proc_dir / "mounts").write_text(
        f"/dev/sda1 {deep} ext4 rw 0 0\n/dev/sda1 {root} ext4 rw 0 0\n", encoding="utf-8"
    )

    found = resources.disks()
    assert len(found) == 1
    assert found[0]["mountpoint"] == str(root)


def test_single_file_mounts_are_excluded(proc_dir, tmp_path):
    """Containers bind-mount individual files such as /etc/hostname. These
    carry no capacity of their own and must never stand in for a filesystem."""
    from sentinel_agent import resources

    real = tmp_path / "data"
    real.mkdir()
    single_file = tmp_path / "hostname"
    single_file.write_text("host\n", encoding="utf-8")

    (proc_dir / "mounts").write_text(
        f"/dev/sda1 {single_file} btrfs rw 0 0\n/dev/sda1 {real} btrfs rw 0 0\n",
        encoding="utf-8",
    )

    found = resources.disks()
    assert len(found) == 1
    assert found[0]["mountpoint"] == str(real)


def test_nonexistent_mountpoint_is_skipped(proc_dir):
    from sentinel_agent import resources

    (proc_dir / "mounts").write_text(
        "/dev/sdz9 /nope/not/here ext4 rw 0 0\n", encoding="utf-8"
    )
    assert resources.disks() == []


def test_sample_includes_every_field(proc_dir):
    from sentinel_agent import resources

    (proc_dir / "stat").write_text(STAT_FIRST, encoding="utf-8")
    (proc_dir / "meminfo").write_text(MEMINFO, encoding="utf-8")
    (proc_dir / "loadavg").write_text("0.1 0.2 0.3 1/1 1\n", encoding="utf-8")
    (proc_dir / "uptime").write_text("100.0 100.0\n", encoding="utf-8")
    (proc_dir / "mounts").write_text("", encoding="utf-8")

    sample = resources.sample()
    assert set(sample) == {
        "cpu_percent",
        "memory_total_bytes",
        "memory_available_bytes",
        "load_1",
        "load_5",
        "load_15",
        "uptime_seconds",
        "disks",
    }
