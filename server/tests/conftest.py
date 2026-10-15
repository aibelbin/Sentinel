"""Test fixtures.

Runs against a scratch database created and dropped per session, so tests never
touch the development data.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

TEST_DB_NAME = f"sentinel_test_{uuid.uuid4().hex[:8]}"

# ---------------------------------------------------------------------------
# DATABASE_URL is redirected HERE, at conftest import time, and not inside a
# fixture. app/db.py builds its engine at module import and app/config.py
# caches settings, so any test module that imports app.db -- directly or
# through app.cli -- would otherwise bind to whatever DATABASE_URL was set
# when pytest collected it. That happened: a test importing app.cli at module
# level caused the suite to migrate and TRUNCATE the production database.
# pytest imports conftest before collecting test modules, so this runs first.
# ---------------------------------------------------------------------------

_ORIGINAL_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://sentinel:sentinel@db:5432/sentinel"
)


def _url_for(database: str) -> str:
    return _ORIGINAL_DATABASE_URL.rsplit("/", 1)[0] + "/" + database


def _admin_url() -> str:
    """Connection to the `postgres` database, for CREATE/DROP DATABASE."""
    return _url_for("postgres")


def _test_url() -> str:
    return _url_for(TEST_DB_NAME)


os.environ["DATABASE_URL"] = _test_url()


def _assert_scratch_database() -> None:
    """Refuse to touch anything but the scratch database.

    A belt to the brace above: if the redirect above ever fails to take effect,
    this fails the suite loudly instead of destroying real data.
    """
    from app.db import engine

    actual = engine.url.database
    if actual != TEST_DB_NAME:
        raise RuntimeError(
            f"tests are pointed at database {actual!r}, not the scratch database "
            f"{TEST_DB_NAME!r}. Refusing to run rather than risk real data."
        )


@pytest.fixture(scope="session", autouse=True)
def _database():
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))

    # Checked before any migration runs, so a misdirected suite cannot migrate
    # or truncate a real database.
    _assert_scratch_database()

    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.upgrade(config, "head")

    yield

    from app.db import engine as app_engine

    app_engine.dispose()
    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
    admin.dispose()


@pytest.fixture
def db(_database):
    from app.db import engine

    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    """Truncate between tests so each one starts from a known state."""
    from app.db import engine

    _assert_scratch_database()
    yield
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE package_change, device_package, inventory_report, task, "
                "device_key, device_resources, device, package, admin_session, "
                "admin_user RESTART IDENTITY CASCADE"
            )
        )
        # agent_config is not truncated: its global row (id 1) is seeded by the
        # migration and every config resolution depends on it.
        conn.execute(
            text(
                "DELETE FROM agent_config WHERE device_id IS NOT NULL; "
                "UPDATE agent_config SET poll_interval_seconds = 60, "
                "inventory_interval_seconds = 43200, spool_max_bytes = 67108864 "
                "WHERE id = 1"
            )
        )


@pytest.fixture
def client(_database):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def admin_token(client, db):
    """A real admin and a real login, so auth is exercised rather than faked."""
    from app.models import AdminUser
    from app.security import hash_password

    db.add(AdminUser(username="tester", password_hash=hash_password("test-password-123")))
    db.commit()

    response = client.post(
        "/api/v1/auth/login", json={"username": "tester", "password": "test-password-123"}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def device_key(client, auth):
    """A created device and its plaintext key."""
    response = client.post("/api/v1/devices", json={"name": "test-device"}, headers=auth)
    assert response.status_code == 201, response.text
    body = response.json()
    return body["device_id"], body["api_key"]


@pytest.fixture
def device_auth(device_key):
    return {"Authorization": f"Bearer {device_key[1]}"}
