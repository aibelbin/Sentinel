from datetime import datetime, timezone

from app.config import get_settings


def derive_status(
    *,
    enabled: bool,
    last_seen_at: datetime | None,
    poll_interval_seconds: int,
    now: datetime | None = None,
) -> str:
    """Device status, computed at read time rather than stored.

    Thresholds are multiples of the resolved poll interval so that changing the
    interval cannot silently break status. Three missed heartbeats before
    `stale` satisfies the requirement that one missed beat must not mark a
    device offline.
    """
    if not enabled:
        return "disabled"
    if last_seen_at is None:
        return "offline"

    now = now or datetime.now(timezone.utc)
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)

    age = (now - last_seen_at).total_seconds()
    settings = get_settings()
    if age <= poll_interval_seconds * settings.stale_after_intervals:
        return "online"
    if age <= poll_interval_seconds * settings.offline_after_intervals:
        return "stale"
    return "offline"
