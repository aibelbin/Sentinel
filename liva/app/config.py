import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    groq_api_key: str | None
    groq_base_url: str
    model: str
    sentinel_url: str
    public_url: str
    db_path: str
    mcp_port: int
    mcp_url: str
    max_steps: int
    retention_days: int


def get_settings() -> Settings:
    sentinel = os.environ.get("SENTINEL_URL", "http://api:8000").rstrip("/")
    port = int(os.environ.get("LIVA_MCP_PORT", "8101"))
    return Settings(
        groq_api_key=os.environ.get("GROQ_API_KEY") or None,
        groq_base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        model=os.environ.get("LIVA_MODEL", "llama-3.3-70b-versatile"),
        sentinel_url=sentinel,
        public_url=os.environ.get("PUBLIC_API_URL", sentinel).rstrip("/"),
        db_path=os.environ.get("LIVA_DB_PATH", "/data/liva.db"),
        mcp_port=port,
        mcp_url=f"http://127.0.0.1:{port}/mcp",
        max_steps=int(os.environ.get("LIVA_MAX_STEPS", "8")),
        retention_days=int(os.environ.get("LIVA_RETENTION_DAYS", "30")),
    )
