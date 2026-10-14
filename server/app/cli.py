"""Operational commands. Run inside the API container.

    python -m app.cli seed
    python -m app.cli reset-admin-password [--password X]
    python -m app.cli prune [--days N]
"""

import argparse
import getpass
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    GLOBAL_CONFIG_ID,
    AdminSession,
    AdminUser,
    AgentConfig,
    InventoryReport,
    PackageChange,
    Task,
)
from app.security import hash_password


def cmd_seed(_args: argparse.Namespace) -> int:
    """Create the administrator and global config row if they do not exist.

    Idempotent, and never overwrites an existing admin: the environment
    variables are a first-boot seed, not a source of truth.
    """
    settings = get_settings()
    with SessionLocal() as db:
        existing = db.execute(select(func.count()).select_from(AdminUser)).scalar_one()
        if existing:
            print("admin already exists, leaving it untouched")
        else:
            db.add(
                AdminUser(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                )
            )
            print(f"created admin '{settings.admin_username}'")

        if db.get(AgentConfig, GLOBAL_CONFIG_ID) is None:
            db.add(AgentConfig(id=GLOBAL_CONFIG_ID, device_id=None))
            print("created global agent config")

        db.commit()
    return 0


def cmd_reset_admin_password(args: argparse.Namespace) -> int:
    """Manual password recovery, per the spec's no-email-flow decision."""
    password = args.password
    if not password:
        password = getpass.getpass("New admin password: ")
        if password != getpass.getpass("Confirm: "):
            print("passwords do not match", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1

    with SessionLocal() as db:
        admin = db.execute(select(AdminUser).order_by(AdminUser.id).limit(1)).scalar_one_or_none()
        if admin is None:
            print("no admin exists; run `seed` first", file=sys.stderr)
            return 1
        username = admin.username
        admin.password_hash = hash_password(password)
        # Every existing session is invalidated, so a stolen token cannot
        # outlive the password it was issued against.
        removed = db.execute(delete(AdminSession)).rowcount
        db.commit()
    print(f"password reset for '{username}'; {removed} session(s) invalidated")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete operational history past the retention window.

    device_package is current state and is never pruned.
    """
    # `or` would turn an explicit --days 0 ("prune everything up to now") into
    # the 30-day default and report success for a prune that did not happen.
    days = args.days if args.days is not None else get_settings().retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        changes = db.execute(
            delete(PackageChange).where(PackageChange.detected_at < cutoff)
        ).rowcount
        reports = db.execute(
            delete(InventoryReport).where(InventoryReport.received_at < cutoff)
        ).rowcount
        tasks = db.execute(delete(Task).where(Task.created_at < cutoff)).rowcount
        sessions = db.execute(delete(AdminSession).where(AdminSession.expires_at < now)).rowcount
        db.commit()

    print(
        f"pruned (older than {days}d): {changes} changes, {reports} reports, "
        f"{tasks} tasks, {sessions} expired sessions"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="create admin and global config if absent").set_defaults(
        func=cmd_seed
    )

    reset = sub.add_parser("reset-admin-password", help="set a new admin password")
    reset.add_argument("--password", help="read from a prompt when omitted")
    reset.set_defaults(func=cmd_reset_admin_password)

    prune = sub.add_parser("prune", help="delete history past the retention window")
    prune.add_argument("--days", type=int, default=None)
    prune.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
