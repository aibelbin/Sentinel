"""Device status derivation. Pure function, no database."""

from datetime import datetime, timedelta, timezone

from app.status import derive_status

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


def status(seconds_ago: float | None, *, enabled: bool = True, interval: int = 60) -> str:
    last_seen = None if seconds_ago is None else NOW - timedelta(seconds=seconds_ago)
    return derive_status(
        enabled=enabled,
        last_seen_at=last_seen,
        poll_interval_seconds=interval,
        now=NOW,
    )


def test_recent_heartbeat_is_online():
    assert status(5) == "online"


def test_one_missed_heartbeat_is_still_online():
    """The requirement: a single missed beat must never mark a device offline."""
    assert status(90) == "online"


def test_two_missed_heartbeats_still_online():
    assert status(150) == "online"


def test_online_boundary_is_inclusive():
    assert status(180) == "online"
    assert status(181) == "stale"


def test_stale_boundary_is_inclusive():
    assert status(900) == "stale"
    assert status(901) == "offline"


def test_never_seen_is_offline():
    assert status(None) == "offline"


def test_disabled_outranks_a_recent_heartbeat():
    assert status(1, enabled=False) == "disabled"


def test_thresholds_scale_with_the_poll_interval():
    """Thresholds are multiples of the interval, so changing it cannot
    silently reclassify healthy devices."""
    assert status(500, interval=300) == "online"
    assert status(900, interval=300) == "online"
    assert status(901, interval=300) == "stale"


def test_naive_timestamp_is_treated_as_utc():
    naive = NOW.replace(tzinfo=None) - timedelta(seconds=10)
    assert derive_status(
        enabled=True, last_seen_at=naive, poll_interval_seconds=60, now=NOW
    ) == "online"
