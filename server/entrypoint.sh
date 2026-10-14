#!/bin/sh
#
# Waits for PostgreSQL, applies migrations, seeds the admin, starts cron, then
# hands off to the given command.
#
set -eu

echo "==> waiting for the database"
attempt=0
until python - <<'PY'
import sys
from sqlalchemy import create_engine, text
from app.config import get_settings

try:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
except Exception as exc:
    print(f"    not ready: {exc}", file=sys.stderr)
    sys.exit(1)
PY
do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo "database did not become ready after 60 attempts" >&2
        exit 1
    fi
    sleep 2
done
echo "    database is ready"

echo "==> applying migrations"
alembic upgrade head

echo "==> seeding"
python -m app.cli seed

# Started here rather than as a second container: one small cron job does not
# justify another service, and it shares this image's code and settings.
echo "==> starting cron"
cron

echo "==> starting: $*"
exec "$@"
