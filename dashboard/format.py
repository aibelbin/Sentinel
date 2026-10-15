"""Presentation helpers. No API or business logic here."""

from datetime import datetime, timezone

STATUS_BADGE = {
    "online": ("🟢", "Online"),
    "stale": ("🟡", "Stale"),
    "offline": ("🔴", "Offline"),
    "disabled": ("⚪", "Disabled"),
}

ECOSYSTEM_LABEL = {
    "os": "OS (apt)",
    "python": "Python",
    "npm": "npm",
    "java": "Java",
}

UPDATE_BADGE = {
    "up to date": "✅ Up to date",
    "update pending": "⏳ Update pending",
    "updating": "⬇️ Updating",
    "rolled back": "↩️ Rolled back",
    "held back": "⛔ Held back",
    "not managed": "➖ Not managed",
    "unknown": "❔ Unknown",
}

CHANGE_LABEL = {
    "added": "➕ added",
    "removed": "➖ removed",
    "version_changed": "🔄 updated",
}


def status_badge(status: str) -> str:
    icon, label = STATUS_BADGE.get(status, ("⚫", status.title()))
    return f"{icon} {label}"


def update_badge(update_status: str | None) -> str:
    if not update_status:
        return UPDATE_BADGE["unknown"]
    return UPDATE_BADGE.get(update_status, update_status)


def short_version(version: str | None) -> str:
    """Versions are source hashes; the first eight characters identify one
    unambiguously in practice and fit in a table."""
    if not version:
        return "—"
    if version.startswith("dev-"):
        return version
    return version[:8]


def human_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def relative_time(value: str | datetime | None) -> str:
    parsed = parse_ts(value)
    if parsed is None:
        return "never"

    seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    if seconds < 0:
        # The device clock is ahead of the server's; say so rather than
        # rendering a negative age.
        return "in the future"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def absolute_time(value: str | datetime | None) -> str:
    parsed = parse_ts(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC") if parsed else "—"


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def memory_used_percent(resources: dict | None) -> float | None:
    if not resources:
        return None
    total = resources.get("memory_total_bytes")
    available = resources.get("memory_available_bytes")
    if not total or available is None:
        return None
    return round(100.0 * (total - available) / total, 1)


def worst_disk_percent(resources: dict | None) -> float | None:
    """The fullest filesystem, which is the one that matters operationally."""
    if not resources:
        return None
    worst: float | None = None
    for disk in resources.get("disks") or []:
        total = disk.get("total_bytes") or 0
        if total <= 0:
            continue
        used = 100.0 * (total - (disk.get("free_bytes") or 0)) / total
        worst = used if worst is None else max(worst, used)
    return round(worst, 1) if worst is not None else None


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"
