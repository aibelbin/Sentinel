"""Admin view of the agent version the fleet should be running."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_admin
from app.models import AgentRelease, Device
from app.schemas import ReleaseOut
from app.services import agent_release, debian_package

router = APIRouter(prefix="/agent-release", tags=["release"], dependencies=[Depends(current_admin)])


def _to_out(db: Session, row: AgentRelease) -> ReleaseOut:
    on_version = db.execute(
        select(func.count()).select_from(Device).where(Device.agent_version == row.version)
    ).scalar_one()
    total = db.execute(select(func.count()).select_from(Device)).scalar_one()
    return ReleaseOut(
        version=row.version,
        sha256=row.sha256,
        first_seen_at=row.first_seen_at,
        quarantined=row.quarantined_at is not None,
        failure_reason=row.failure_reason,
        devices_on_version=on_version,
        devices_total=total,
        package_filename=debian_package.filename(row.version),
    )


@router.get("", response_model=ReleaseOut)
def get_release(db: Session = Depends(get_db)) -> ReleaseOut:
    """The version this server is serving, and how much of the fleet has it."""
    row = agent_release.record(db)
    db.commit()
    return _to_out(db, row)


@router.post("/clear-quarantine", response_model=ReleaseOut)
def clear_quarantine(db: Session = Depends(get_db)) -> ReleaseOut:
    """Let a quarantined version be handed out again.

    For when the failure was a fluke rather than the build. Pushing a new
    version clears quarantine on its own, since it applies to one hash.
    """
    version, _sha256 = agent_release.current_release()
    if not agent_release.clear_quarantine(db, version):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "not_quarantined", "message": f"{version} is not quarantined"},
        )
    db.commit()
    row = db.get(AgentRelease, version)
    return _to_out(db, row)
