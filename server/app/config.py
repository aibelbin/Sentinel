from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment.

    Every value has a development default so the stack boots with an empty
    .env; production supplies real values through Compose.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://sentinel:sentinel@db:5432/sentinel"

    # Seeded on first start only; ignored once an admin row exists.
    admin_username: str = "admin"
    admin_password: str = "change-me-now"

    session_ttl_hours: int = 12
    retention_days: int = 30
    abandoned_task_minutes: int = 30

    # Multiples of the resolved poll interval. See spec section 7.3.
    stale_after_intervals: int = 3
    offline_after_intervals: int = 15

    # Directory holding the agent source that gets bundled into the installer.
    agent_source_dir: str = "/app/agent"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
