"""Serves the self-contained installer script to the dashboard."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.deps import current_admin
from app.schemas import InstallerRequest
from app.services import agent_release
from app.services.installer import build_installer

router = APIRouter(tags=["installer"], dependencies=[Depends(current_admin)])

_MEDIA_TYPE = "text/x-shellscript; charset=utf-8"


def _render(body: InstallerRequest) -> str:
    version, _sha256 = agent_release.current_release()
    try:
        return build_installer(
            get_settings().agent_source_dir,
            version=version,
            server_url=body.server_url,
            api_key=body.api_key,
            insecure=body.insecure,
        )
    except (FileNotFoundError, ValueError) as exc:
        # A broken bundle is a deployment fault, not a client error.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "installer_unavailable", "message": str(exc)},
        ) from exc


@router.get("/installer", response_class=PlainTextResponse)
def get_installer() -> PlainTextResponse:
    """Generic installer: prompts for the server URL and key when run."""
    return PlainTextResponse(
        _render(InstallerRequest()),
        media_type=_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="sentinel-install.sh"'},
    )


@router.post("/installer", response_class=PlainTextResponse)
def post_installer(body: InstallerRequest) -> PlainTextResponse:
    """Installer with the server URL and key pre-filled.

    A POST rather than a GET with query parameters so the key does not appear
    in access logs or browser history.
    """
    return PlainTextResponse(
        _render(body),
        media_type=_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="sentinel-install.sh"'},
    )
