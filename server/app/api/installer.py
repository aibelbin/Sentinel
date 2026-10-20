"""Serves the ways a watchdog can be installed: a .deb, and a self-contained
script for machines that cannot use apt."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse, Response

from app.config import get_settings
from app.deps import current_admin
from app.schemas import InstallerRequest
from app.services import watchdog_release, debian_package
from app.services.installer import build_installer

router = APIRouter(tags=["installer"], dependencies=[Depends(current_admin)])

_MEDIA_TYPE = "text/x-shellscript; charset=utf-8"


def _render(body: InstallerRequest) -> str:
    version, _sha256 = watchdog_release.current_release()
    try:
        return build_installer(
            get_settings().watchdog_source_dir,
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


@router.get("/watchdog-package")
def get_watchdog_package() -> Response:
    """The .deb, which is the normal way to install.

    apt resolves python3 and cron from it, so the target machine needs no
    preparation. It carries no credentials -- the package is identical for
    every machine -- so enrolment is a separate step afterwards.
    """
    version, _sha256 = watchdog_release.current_release()
    try:
        data = debian_package.build(get_settings().watchdog_source_dir, version)
    except (debian_package.PackageBuildFailed, FileNotFoundError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "package_unavailable", "message": str(exc)},
        ) from exc

    return Response(
        content=data,
        media_type="application/vnd.debian.binary-package",
        headers={
            "Content-Disposition": f'attachment; filename="{debian_package.filename(version)}"',
            "X-Watchdog-Version": version,
        },
    )


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
