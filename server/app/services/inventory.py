"""Inventory ingestion: normalization, diffing and change-log generation.

This is the only place that writes `device_package` and `package_change`, and
it runs entirely inside one transaction so a device's current state and its
change log can never disagree.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import (
    ChangeType,
    Device,
    DevicePackage,
    Ecosystem,
    InventoryReport,
    Package,
    PackageChange,
)
from app.schemas import HostFacts, InventoryRequest

log = logging.getLogger(__name__)

_PYTHON_SEPARATORS = re.compile(r"[-_.]+")


def normalize_name(ecosystem: str, name: str) -> str:
    """Ecosystem-specific identity for a package name.

    Python follows PEP 503: lowercase, with runs of `-`, `_` and `.` collapsed
    to a single `-`, so `Flask_SQLAlchemy` and `flask-sqlalchemy` are one
    package. The others are case-folded only, since their registries treat
    punctuation as significant.
    """
    name = name.strip()
    if ecosystem == Ecosystem.python.value:
        return _PYTHON_SEPARATORS.sub("-", name).lower()
    return name.lower()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def apply_host_facts(device: Device, host: HostFacts | None) -> None:
    """Refresh host metadata. Never used for identity or matching."""
    if host is None:
        return
    for field, value in host.model_dump(exclude_none=True).items():
        setattr(device, field, value)


def _resolve_package_ids(db: Session, wanted: dict[tuple[str, str], str]) -> dict[tuple[str, str], int]:
    """Map (ecosystem, normalized_name) -> package.id, inserting what is new.

    `wanted` values are display names, used only when a row has to be created.
    """
    if not wanted:
        return {}

    by_ecosystem: dict[str, list[str]] = defaultdict(list)
    for ecosystem, normalized in wanted:
        by_ecosystem[ecosystem].append(normalized)

    resolved: dict[tuple[str, str], int] = {}
    for ecosystem, names in by_ecosystem.items():
        rows = db.execute(
            select(Package.id, Package.normalized_name).where(
                Package.ecosystem == Ecosystem(ecosystem),
                Package.normalized_name.in_(names),
            )
        ).all()
        for package_id, normalized in rows:
            resolved[(ecosystem, normalized)] = package_id

    missing = [key for key in wanted if key not in resolved]
    if not missing:
        return resolved

    # on_conflict_do_nothing makes this safe against two devices reporting the
    # same new package at the same moment.
    db.execute(
        pg_insert(Package)
        .values(
            [
                {
                    "ecosystem": Ecosystem(ecosystem),
                    "normalized_name": normalized,
                    "name": wanted[(ecosystem, normalized)],
                }
                for ecosystem, normalized in missing
            ]
        )
        .on_conflict_do_nothing(index_elements=["ecosystem", "normalized_name"])
    )
    db.flush()

    by_ecosystem_missing: dict[str, list[str]] = defaultdict(list)
    for ecosystem, normalized in missing:
        by_ecosystem_missing[ecosystem].append(normalized)
    for ecosystem, names in by_ecosystem_missing.items():
        rows = db.execute(
            select(Package.id, Package.normalized_name).where(
                Package.ecosystem == Ecosystem(ecosystem),
                Package.normalized_name.in_(names),
            )
        ).all()
        for package_id, normalized in rows:
            resolved[(ecosystem, normalized)] = package_id

    return resolved


def apply_inventory(db: Session, device: Device, req: InventoryRequest) -> InventoryReport:
    """Store a report and reconcile it against the device's current state."""
    collected_at = _as_utc(req.collected_at)
    now = datetime.now(timezone.utc)

    # A device clock set forward would otherwise become a permanent high-water
    # mark: every later, correctly-stamped report would compare as older and be
    # discarded as superseded, freezing that device's inventory for good with no
    # in-app way to recover. Clamping keeps data flowing; the dashboard shows
    # collected_at beside received_at, so the skew stays visible.
    if collected_at > now:
        log.warning(
            "device %s reported collected_at %s ahead of server time %s; clamping",
            device.id,
            collected_at.isoformat(),
            now.isoformat(),
        )
        collected_at = now

    newest_applied = db.execute(
        select(func.max(InventoryReport.collected_at)).where(
            InventoryReport.device_id == device.id,
            InventoryReport.superseded.is_(False),
        )
    ).scalar_one_or_none()

    # A report older than one already applied must not be diffed: comparing it
    # against newer current state would manufacture false changes. It is still
    # recorded, flagged, and counted.
    superseded = newest_applied is not None and collected_at <= _as_utc(newest_applied)

    report = InventoryReport(
        device_id=device.id,
        collected_at=collected_at,
        received_at=now,
        package_count=len(req.packages),
        change_count=0,
        collector_status=req.collector_status,
        superseded=superseded,
    )
    db.add(report)
    db.flush()

    apply_host_facts(device, req.host)

    if superseded:
        return report

    # Deduplicate within the report: last entry wins for a given identity.
    desired: dict[tuple[str, str, str], tuple[str, str, dict]] = {}
    display_names: dict[tuple[str, str], str] = {}
    for item in req.packages:
        normalized = normalize_name(item.ecosystem, item.name)
        if not normalized:
            continue
        display_names.setdefault((item.ecosystem, normalized), item.name)
        desired[(item.ecosystem, normalized, item.scope)] = (item.version, item.name, item.meta)

    package_ids = _resolve_package_ids(db, display_names)

    desired_by_row: dict[tuple[int, str], tuple[str, dict]] = {}
    for (ecosystem, normalized, scope), (version, _name, meta) in desired.items():
        package_id = package_ids.get((ecosystem, normalized))
        if package_id is not None:
            desired_by_row[(package_id, scope)] = (version, meta)

    current_rows = db.execute(
        select(DevicePackage).where(DevicePackage.device_id == device.id)
    ).scalars().all()
    current = {(row.package_id, row.scope): row for row in current_rows}

    changes: list[PackageChange] = []

    for key, (version, meta) in desired_by_row.items():
        package_id, scope = key
        existing = current.get(key)
        if existing is None:
            db.add(
                DevicePackage(
                    device_id=device.id,
                    package_id=package_id,
                    scope=scope,
                    version=version,
                    first_seen_at=collected_at,
                    last_seen_at=collected_at,
                    meta=meta,
                )
            )
            changes.append(
                PackageChange(
                    device_id=device.id,
                    package_id=package_id,
                    scope=scope,
                    change_type=ChangeType.added,
                    old_version=None,
                    new_version=version,
                    detected_at=collected_at,
                    report_id=report.id,
                )
            )
        else:
            if existing.version != version:
                changes.append(
                    PackageChange(
                        device_id=device.id,
                        package_id=package_id,
                        scope=scope,
                        change_type=ChangeType.version_changed,
                        old_version=existing.version,
                        new_version=version,
                        detected_at=collected_at,
                        report_id=report.id,
                    )
                )
                existing.version = version
            existing.last_seen_at = collected_at
            existing.meta = meta

    for key, row in current.items():
        if key in desired_by_row:
            continue
        package_id, scope = key
        changes.append(
            PackageChange(
                device_id=device.id,
                package_id=package_id,
                scope=scope,
                change_type=ChangeType.removed,
                old_version=row.version,
                new_version=None,
                detected_at=collected_at,
                report_id=report.id,
            )
        )
        db.delete(row)

    db.add_all(changes)
    report.change_count = len(changes)
    db.flush()
    return report
