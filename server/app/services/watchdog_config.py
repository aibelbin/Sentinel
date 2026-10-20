"""Watchdog configuration resolution: per-device override, else global default."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GLOBAL_CONFIG_ID, WatchdogConfig
from app.schemas import WatchdogConfigOut


def get_global(db: Session) -> WatchdogConfig:
    """The single global row, created on first access if the seed never ran."""
    row = db.get(WatchdogConfig, GLOBAL_CONFIG_ID)
    if row is None:
        row = WatchdogConfig(id=GLOBAL_CONFIG_ID, device_id=None)
        db.add(row)
        db.flush()
    return row


def _to_out(row: WatchdogConfig) -> WatchdogConfigOut:
    return WatchdogConfigOut(
        poll_interval_seconds=row.poll_interval_seconds,
        inventory_interval_seconds=row.inventory_interval_seconds,
        spool_max_bytes=row.spool_max_bytes,
    )


def resolve(db: Session, device_id: uuid.UUID) -> WatchdogConfigOut:
    override = db.execute(
        select(WatchdogConfig).where(WatchdogConfig.device_id == device_id)
    ).scalar_one_or_none()
    return _to_out(override or get_global(db))


def resolve_many(db: Session, device_ids: list[uuid.UUID]) -> dict[uuid.UUID, WatchdogConfigOut]:
    """Batch resolution, so listing N devices costs two queries rather than N."""
    default = _to_out(get_global(db))
    if not device_ids:
        return {}

    overrides = db.execute(
        select(WatchdogConfig).where(WatchdogConfig.device_id.in_(device_ids))
    ).scalars().all()
    by_device = {row.device_id: _to_out(row) for row in overrides}
    return {device_id: by_device.get(device_id, default) for device_id in device_ids}
