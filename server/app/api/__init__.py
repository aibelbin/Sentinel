from fastapi import APIRouter

from app.api import agent, auth, devices, installer, settings

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(agent.router)
api_router.include_router(devices.router)
api_router.include_router(installer.router)
api_router.include_router(settings.router)
