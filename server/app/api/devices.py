"""Admin routes for device management and inspection."""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_admin
from app.models import (
    AdminUser,
    WatchdogRelease,
    Device,
    DeviceKey,
    DevicePackage,
    DeviceResources,
    Ecosystem,
    InventoryReport,
    Package,
    PackageChange,
    Task,
    TaskStatus,
)
from app.schemas import (
    ChangeOut,
    DeviceCreate,
    DeviceDetail,
    DeviceKeyIssued,
    DevicePatch,
    DeviceSummary,
    Disk,
    Page,
    PackageOut,
    ReportOut,
    ResourcesOut,
    TaskCreate,
    TaskHistoryOut,
)
from app.security import new_device_key
from app.services import watchdog_config, watchdog_release
from app.status import derive_status

router = APIRouter(prefix="/devices", tags=["devices"], dependencies=[Depends(current_admin)])


def _resources_out(row: DeviceResources | None) -> ResourcesOut | None:
    if row is None:
        return None
    return ResourcesOut(
        sampled_at=row.sampled_at,
        cpu_percent=row.cpu_percent,
        memory_total_bytes=row.memory_total_bytes,
        memory_available_bytes=row.memory_available_bytes,
        load_1=row.load_1,
        load_5=row.load_5,
        load_15=row.load_15,
        uptime_seconds=row.uptime_seconds,
        disks=[Disk(**disk) for disk in (row.disks or [])],
    )


def _get_device(db: Session, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "no_such_device"}
        )
    return device


def _package_counts(db: Session, device_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not device_ids:
        return {}
    rows = db.execute(
        select(DevicePackage.device_id, func.count())
        .where(DevicePackage.device_id.in_(device_ids))
        .group_by(DevicePackage.device_id)
    ).all()
    return {device_id: count for device_id, count in rows}


@router.get("", response_model=list[DeviceSummary])
def list_devices(
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=128),
) -> list[DeviceSummary]:
    stmt = select(Device).order_by(Device.name)
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Device.name).like(pattern)
            | func.lower(func.coalesce(Device.hostname, "")).like(pattern)
        )

    devices = db.execute(stmt).scalars().all()
    device_ids = [device.id for device in devices]
    configs = watchdog_config.resolve_many(db, device_ids)
    counts = _package_counts(db, device_ids)
    resources = {
        row.device_id: row
        for row in db.execute(
            select(DeviceResources).where(DeviceResources.device_id.in_(device_ids))
        ).scalars().all()
    } if device_ids else {}

    release = db.get(WatchdogRelease, watchdog_release.current_release()[0])

    now = datetime.now(timezone.utc)
    out: list[DeviceSummary] = []
    for device in devices:
        derived = derive_status(
            enabled=device.enabled,
            last_seen_at=device.last_seen_at,
            poll_interval_seconds=configs[device.id].poll_interval_seconds,
            now=now,
        )
        if status_filter and derived != status_filter:
            continue
        out.append(
            DeviceSummary(
                id=device.id,
                name=device.name,
                status=derived,
                enabled=device.enabled,
                created_at=device.created_at,
                last_seen_at=device.last_seen_at,
                watchdog_version=device.watchdog_version,
                hostname=device.hostname,
                os_name=device.os_name,
                os_version=device.os_version,
                package_count=counts.get(device.id, 0),
                resources=_resources_out(resources.get(device.id)),
                update_status=watchdog_release.update_status(device, release),
            )
        )
    return out


@router.post("", response_model=DeviceKeyIssued, status_code=status.HTTP_201_CREATED)
def create_device(body: DeviceCreate, db: Session = Depends(get_db)) -> DeviceKeyIssued:
    """Create a device and issue its key.

    The plaintext key is returned here and nowhere else: only its SHA-256 hash
    is stored, so it cannot be recovered later. Rotation issues a new one.
    """
    device = Device(name=body.name.strip())
    db.add(device)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "duplicate_name", "message": "A device with that name exists."},
        ) from exc

    plaintext, key_hash = new_device_key()
    db.add(DeviceKey(device_id=device.id, key_hash=key_hash))
    db.commit()

    return DeviceKeyIssued(device_id=device.id, name=device.name, api_key=plaintext)


@router.get("/{device_id}", response_model=DeviceDetail)
def get_device(device_id: uuid.UUID, db: Session = Depends(get_db)) -> DeviceDetail:
    device = _get_device(db, device_id)
    config = watchdog_config.resolve(db, device.id)
    count = _package_counts(db, [device.id]).get(device.id, 0)
    target_version = watchdog_release.current_release()[0]
    release = db.get(WatchdogRelease, target_version)

    last_report = db.execute(
        select(InventoryReport)
        .where(InventoryReport.device_id == device.id)
        .order_by(InventoryReport.received_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    return DeviceDetail(
        id=device.id,
        name=device.name,
        status=derive_status(
            enabled=device.enabled,
            last_seen_at=device.last_seen_at,
            poll_interval_seconds=config.poll_interval_seconds,
        ),
        enabled=device.enabled,
        created_at=device.created_at,
        last_seen_at=device.last_seen_at,
        watchdog_version=device.watchdog_version,
        hostname=device.hostname,
        os_name=device.os_name,
        os_version=device.os_version,
        package_count=count,
        resources=_resources_out(db.get(DeviceResources, device.id)),
        kernel=device.kernel,
        arch=device.arch,
        cpu_model=device.cpu_model,
        cpu_cores=device.cpu_cores,
        total_memory_bytes=device.total_memory_bytes,
        config=config,
        last_report_at=last_report.collected_at if last_report else None,
        last_collector_status=last_report.collector_status if last_report else {},
        update_status=watchdog_release.update_status(device, release),
        previous_watchdog_version=device.previous_watchdog_version,
        last_update_at=device.last_update_at,
        last_update_result=device.last_update_result,
        last_update_detail=device.last_update_detail,
        target_watchdog_version=target_version,
    )


@router.patch("/{device_id}", response_model=DeviceDetail)
def patch_device(
    device_id: uuid.UUID, body: DevicePatch, db: Session = Depends(get_db)
) -> DeviceDetail:
    device = _get_device(db, device_id)
    if body.name is not None:
        device.name = body.name.strip()
    if body.enabled is not None:
        device.enabled = body.enabled
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"code": "duplicate_name"}
        ) from exc
    return get_device(device_id, db)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    device = _get_device(db, device_id)
    db.delete(device)
    db.commit()


@router.post("/{device_id}/rotate-key", response_model=DeviceKeyIssued)
def rotate_key(device_id: uuid.UUID, db: Session = Depends(get_db)) -> DeviceKeyIssued:
    """Revoke the live key and issue a new one, in one transaction.

    The old key row is kept with a revoked_at timestamp rather than deleted, so
    the rotation history survives.
    """
    device = _get_device(db, device_id)
    now = datetime.now(timezone.utc)

    live = db.execute(
        select(DeviceKey).where(DeviceKey.device_id == device.id, DeviceKey.revoked_at.is_(None))
    ).scalars().all()
    for row in live:
        row.revoked_at = now
    db.flush()

    plaintext, key_hash = new_device_key()
    db.add(DeviceKey(device_id=device.id, key_hash=key_hash, created_at=now))
    db.commit()

    return DeviceKeyIssued(device_id=device.id, name=device.name, api_key=plaintext)


@router.get("/{device_id}/packages", response_model=Page[PackageOut])
def list_packages(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    ecosystem: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> Page[PackageOut]:
    _get_device(db, device_id)

    base = (
        select(DevicePackage, Package)
        .join(Package, Package.id == DevicePackage.package_id)
        .where(DevicePackage.device_id == device_id)
    )
    if ecosystem:
        if ecosystem not in {item.value for item in Ecosystem}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "bad_ecosystem"}
            )
        base = base.where(Package.ecosystem == Ecosystem(ecosystem))
    if q:
        base = base.where(Package.normalized_name.like(f"%{q.strip().lower()}%"))

    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = db.execute(
        base.order_by(Package.ecosystem, Package.normalized_name, DevicePackage.scope)
        .limit(limit)
        .offset(offset)
    ).all()

    return Page[PackageOut](
        items=[
            PackageOut(
                ecosystem=package.ecosystem.value,
                name=package.name,
                version=device_package.version,
                scope=device_package.scope,
                first_seen_at=device_package.first_seen_at,
                last_seen_at=device_package.last_seen_at,
            )
            for device_package, package in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{device_id}/changes", response_model=Page[ChangeOut])
def list_changes(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> Page[ChangeOut]:
    _get_device(db, device_id)

    base = (
        select(PackageChange, Package)
        .join(Package, Package.id == PackageChange.package_id)
        .where(PackageChange.device_id == device_id)
    )
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = db.execute(
        base.order_by(PackageChange.detected_at.desc(), PackageChange.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return Page[ChangeOut](
        items=[
            ChangeOut(
                id=change.id,
                ecosystem=package.ecosystem.value,
                name=package.name,
                scope=change.scope,
                change_type=change.change_type.value,
                old_version=change.old_version,
                new_version=change.new_version,
                detected_at=change.detected_at,
            )
            for change, package in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{device_id}/reports", response_model=Page[ReportOut])
def list_reports(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[ReportOut]:
    _get_device(db, device_id)

    total = db.execute(
        select(func.count()).select_from(InventoryReport).where(
            InventoryReport.device_id == device_id
        )
    ).scalar_one()
    rows = db.execute(
        select(InventoryReport)
        .where(InventoryReport.device_id == device_id)
        .order_by(InventoryReport.received_at.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()

    return Page[ReportOut](
        items=[
            ReportOut(
                id=row.id,
                collected_at=row.collected_at,
                received_at=row.received_at,
                package_count=row.package_count,
                change_count=row.change_count,
                superseded=row.superseded,
                collector_status=row.collector_status,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{device_id}/tasks", response_model=Page[TaskHistoryOut])
def list_tasks(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[TaskHistoryOut]:
    _get_device(db, device_id)

    total = db.execute(
        select(func.count()).select_from(Task).where(Task.device_id == device_id)
    ).scalar_one()
    rows = db.execute(
        select(Task)
        .where(Task.device_id == device_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=get_settings().abandoned_task_minutes
    )
    return Page[TaskHistoryOut](
        items=[
            TaskHistoryOut(
                id=row.id,
                type=row.type,
                status=row.status.value,
                created_at=row.created_at,
                claimed_at=row.claimed_at,
                completed_at=row.completed_at,
                result=row.result,
                error=row.error,
                # Surfaced rather than auto-retried: v1 shows the truth and
                # lets the administrator decide.
                abandoned=(
                    row.status == TaskStatus.running
                    and row.claimed_at is not None
                    and row.claimed_at < cutoff
                ),
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{device_id}/tasks", response_model=TaskHistoryOut, status_code=status.HTTP_201_CREATED)
def queue_task(
    device_id: uuid.UUID, body: TaskCreate, db: Session = Depends(get_db)
) -> TaskHistoryOut:
    device = _get_device(db, device_id)
    if not device.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "device_disabled", "message": "Enable the device before queueing."},
        )

    task = Task(device_id=device.id, type=body.type, params=body.params)
    db.add(task)
    db.commit()

    return TaskHistoryOut(
        id=task.id,
        type=task.type,
        status=task.status.value,
        created_at=task.created_at,
        claimed_at=None,
        completed_at=None,
        result=None,
        error=None,
        abandoned=False,
    )
