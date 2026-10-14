from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_admin
from app.models import AdminSession, AdminUser
from app.schemas import LoginRequest, LoginResponse
from app.security import new_session_token, sha256_hex, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    admin = db.execute(
        select(AdminUser).where(AdminUser.username == body.username)
    ).scalar_one_or_none()

    # Verify even when the user is unknown would be ideal for timing, but a
    # single-admin system with no enumeration surface does not warrant the
    # complexity. The generic message is still used.
    if admin is None or not verify_password(body.password, admin.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "Incorrect username or password."},
        )

    now = datetime.now(timezone.utc)
    token, token_hash = new_session_token()
    expires_at = now + timedelta(hours=get_settings().session_ttl_hours)

    db.add(
        AdminSession(
            token_hash=token_hash,
            admin_user_id=admin.id,
            created_at=now,
            expires_at=expires_at,
            last_used_at=now,
        )
    )
    admin.last_login_at = now
    db.commit()

    return LoginResponse(token=token, expires_at=expires_at, username=admin.username)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(current_admin),
) -> None:
    if authorization and authorization.lower().startswith("bearer "):
        token_hash = sha256_hex(authorization.split(" ", 1)[1].strip())
        session = db.get(AdminSession, token_hash)
        if session is not None:
            db.delete(session)
            db.commit()
