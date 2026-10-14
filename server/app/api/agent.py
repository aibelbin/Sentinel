"""Routes the client agent calls. Authenticated by per-device API key."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_device
from app.models import Device, DeviceResources, Task, TaskStatus
from app.schemas import (
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    InventoryRequest,
    InventoryResponse,
    TaskOut,
    TaskResultRequest,
)
from app.services import agent_config
from app.services.inventory import apply_host_facts, apply_inventory

router = APIRouter(prefix="/agent", tags=["agent"])

# Task types the platform will ever hand to an agent. The agent validates this
# again on its side; both ends refusing unknown types is deliberate.
ALLOWED_TASK_TYPES = {"collect_inventory"}


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
    db.commit()
