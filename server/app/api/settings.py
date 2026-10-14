from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_admin
from app.schemas import AgentConfigOut, ConfigUpdate
from app.services import agent_config

router = APIRouter(tags=["settings"], dependencies=[Depends(current_admin)])


@router.get("/config", response_model=AgentConfigOut)
def get_config(db: Session = Depends(get_db)) -> AgentConfigOut:
    row = agent_config.get_global(db)
    db.commit()
    return AgentConfigOut(
        poll_interval_seconds=row.poll_interval_seconds,
        inventory_interval_seconds=row.inventory_interval_seconds,
        spool_max_bytes=row.spool_max_bytes,
    )


@router.put("/config", response_model=AgentConfigOut)
def put_config(body: ConfigUpdate, db: Session = Depends(get_db)) -> AgentConfigOut:
    """Update the global defaults.

    Agents converge on the next heartbeat; nothing is pushed to devices.
    """
    row = agent_config.get_global(db)
    row.poll_interval_seconds = body.poll_interval_seconds
    row.inventory_interval_seconds = body.inventory_interval_seconds
    row.spool_max_bytes = body.spool_max_bytes
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return AgentConfigOut(**body.model_dump())
