from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AdminSession, AdminUser, Device, DeviceKey
from app.security import sha256_hex


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()


def current_admin(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AdminUser:
    token_hash = sha256_hex(_bearer(authorization))
    now = datetime.now(timezone.utc)

    session = db.get(AdminSession, token_hash)
    if session is None or session.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_session"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Sliding expiry: every authenticated call extends the window.
    session.last_used_at = now
    session.expires_at = now + timedelta(hours=get_settings().session_ttl_hours)

    admin = db.get(AdminUser, session.admin_user_id)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_session"}
        )
    db.commit()
    return admin


def current_device(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Device:
    key_hash = sha256_hex(_bearer(authorization))

    row = db.execute(
        select(DeviceKey).where(DeviceKey.key_hash == key_hash, DeviceKey.revoked_at.is_(None))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": "invalid_or_revoked_key"}
        )

    device = db.get(Device, row.device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "no_such_device"})
    if not device.enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "device_disabled"})

    row.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return device
