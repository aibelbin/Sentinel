"""Routes the client agent calls. Authenticated by per-device API key."""

import base64
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_device
from app.config import get_settings
from app.models import Device, DeviceResources, Task, TaskStatus
from app.schemas import (
    AgentBundle,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    InventoryRequest,
    InventoryResponse,
    TaskOut,
    TaskResultRequest,
    UpdateReport,
)
from app.services import agent_config, agent_release
from app.services.installer import bundle_bytes
from app.services.inventory import apply_host_facts, apply_inventory

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/enroll", response_model=EnrollResponse)
def enroll(
    body: EnrollRequest,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> EnrollResponse:
    """Attach host facts to a pre-created device and return its platform ID.

    Idempotent: re-running the installer with the same key returns the same
    device ID and simply refreshes the host facts.
    """
    apply_host_facts(device, body.host)
    device.agent_version = body.agent_version
    db.commit()

    return EnrollResponse(
        device_id=device.id,
        name=device.name,
        config=agent_config.resolve(db, device.id),
    )


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    body: HeartbeatRequest,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> HeartbeatResponse:
    now = datetime.now(timezone.utc)

    device.last_seen_at = now
    device.agent_version = body.agent_version
    apply_host_facts(device, body.host)

    sample = body.resources
    row = db.get(DeviceResources, device.id)
    if row is None:
        row = DeviceResources(device_id=device.id, sampled_at=now)
        db.add(row)
    row.sampled_at = now
    row.cpu_percent = sample.cpu_percent
    row.memory_total_bytes = sample.memory_total_bytes
    row.memory_available_bytes = sample.memory_available_bytes
    row.load_1 = sample.load_1
    row.load_5 = sample.load_5
    row.load_15 = sample.load_15
    row.uptime_seconds = sample.uptime_seconds
    row.disks = [disk.model_dump() for disk in sample.disks]

    _apply_update_report(db, device, body.update)

    # Queued here rather than on a schedule: the heartbeat is already the one
    # moment the server knows what a machine is running.
    agent_release.maybe_queue_update(db, device)

    # Handing a task out transitions it to `running`, so the next beat does not
    # deliver it again.
    pending = db.execute(
        select(Task)
        .where(Task.device_id == device.id, Task.status == TaskStatus.pending)
        .order_by(Task.created_at)
        .limit(20)
    ).scalars().all()
    for task in pending:
        task.status = TaskStatus.running
        task.claimed_at = now

    db.commit()

    return HeartbeatResponse(
        config=agent_config.resolve(db, device.id),
        tasks=[TaskOut(id=task.id, type=task.type, params=task.params) for task in pending],
        server_time=now,
    )


def _apply_update_report(db: Session, device: Device, report: UpdateReport | None) -> None:
    """Record how the machine's last update went, if it told us.

    A revert is the only signal that a version installed cleanly and then could
    not run. The job was reported *completed* -- the swap worked -- so without
    treating this as a failure the server would keep handing the same broken
    version to the same machine every few minutes, and to the rest of the
    fleet besides.
    """
    if report is None or report.result is None:
        return

    if report.result == "reverted" and report.version:
        agent_release.quarantine(
            db,
            report.version,
            device.id,
            report.detail or "installed but could not complete a beat; the machine reverted",
        )
    # "ok" is the steady state a machine reports on every beat once a version
    # is proven; only write when something actually changed, so last_update_at
    # keeps meaning "when the last update happened".
    if device.last_update_result == report.result and device.last_update_detail == report.detail:
        return
    device.last_update_result = report.result
    device.last_update_detail = report.detail
    device.last_update_at = report.at or datetime.now(timezone.utc)
    if report.result in ("installed", "reverted") and report.version:
        device.previous_agent_version = report.version


@router.get("/bundle", response_model=AgentBundle)
def get_bundle(
    version: str,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> AgentBundle:
    """Serve the agent code for an update.

    Only the current version is served: an agent asking for anything else is
    working from a stale job, and handing it older code would fight the
    server's own view of what should be running.
    """
    current_version, sha256 = agent_release.current_release()
    if version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "version_not_current",
                "message": f"{version} is not the current version ({current_version})",
            },
        )

    release = agent_release.record(db)
    if release.quarantined_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "version_quarantined", "message": release.failure_reason or ""},
        )
    db.commit()

    raw = bundle_bytes(get_settings().agent_source_dir)
    return AgentBundle(
        version=current_version,
        sha256=sha256,
        bundle_b64=base64.b64encode(raw).decode("ascii"),
    )


@router.post("/inventory", response_model=InventoryResponse)
def submit_inventory(
    body: InventoryRequest,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> InventoryResponse:
    report = apply_inventory(db, device, body)
    db.commit()
    return InventoryResponse(
        report_id=report.id,
        change_count=report.change_count,
        package_count=report.package_count,
        superseded=report.superseded,
    )


@router.post("/tasks/{task_id}/result", status_code=status.HTTP_204_NO_CONTENT)
def submit_task_result(
    task_id: uuid.UUID,
    body: TaskResultRequest,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> None:
    task = db.get(Task, task_id)
    # Not found and belonging-to-another-device are the same response, so a
    # device cannot probe for other devices' task IDs.
    if task is None or task.device_id != device.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "no_such_task"}
        )

    task.status = TaskStatus.completed if body.status == "completed" else TaskStatus.failed
    task.result = body.result
    task.error = body.error
    task.completed_at = datetime.now(timezone.utc)

    # The first machine to report an update failure stops that version being
    # handed to anyone else, so one machine has a bad minute instead of the
    # whole fleet going dark.
    if task.type == agent_release.UPDATE_TASK_TYPE and task.status == TaskStatus.failed:
        version = (task.params or {}).get("version")
        kind = (body.result or {}).get("kind")
        if version and agent_release.is_build_failure(kind, body.error or ""):
            agent_release.quarantine(db, version, device.id, body.error or "")
        elif version:
            # A fetch timeout on one machine says nothing about the release, so
            # it stays available to everyone else.
            logging.getLogger(__name__).info(
                "not quarantining %s: failure on device %s was %s, not the build",
                version, device.id, kind or "untyped",
            )

    db.commit()
