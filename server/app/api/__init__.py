from fastapi import APIRouter

from app.api import watchdog, auth, devices, installer, release, settings

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(watchdog.router)
api_router.include_router(watchdog.legacy_router)
api_router.include_router(devices.router)
api_router.include_router(installer.router)
api_router.include_router(release.router)
api_router.include_router(settings.router)
