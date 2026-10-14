import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import api_router
from app.config import get_settings
from app.db import engine

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="Sentinel",
    version="1.0.0",
    description=(
        "Device inventory and management platform. Agent routes authenticate "
        "with a per-device API key; admin routes with a session bearer token."
    ),
)
app.include_router(api_router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    """Liveness plus a database round-trip, for Compose health checks."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - reported, not raised, to the probe
        logging.getLogger(__name__).warning("health check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "degraded", "database": False})
    return JSONResponse(content={"status": "ok", "database": True})
