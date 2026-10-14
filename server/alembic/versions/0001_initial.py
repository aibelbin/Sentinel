"""Initial schema: admin auth, devices, inventory and tasks.

Revision ID: 0001_initial
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False so the explicit .create() calls in upgrade() are the only
# place these types are created; otherwise create_table emits CREATE TYPE again
# and the migration fails on the second table that references one.
ECOSYSTEM = postgresql.ENUM(
    "os", "python", "npm", "java", name="ecosystem", create_type=False
)
CHANGE_TYPE = postgresql.ENUM(
    "added", "removed", "version_changed", name="change_type", create_type=False
)
TASK_STATUS = postgresql.ENUM(
    "pending", "running", "completed", "failed", name="task_status", create_type=False
)

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    ECOSYSTEM.create(bind, checkfirst=True)
    CHANGE_TYPE.create(bind, checkfirst=True)
    TASK_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "admin_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(128), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("last_login_at", TS, nullable=True),
    )

    op.create_table(
        "admin_session",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "admin_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
    )

    op.create_table(
        "device",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_seen_at", TS, nullable=True),
        sa.Column("agent_version", sa.String(32), nullable=True),
        sa.Column("hostname", sa.String(255), nullable=True),
        sa.Column("os_name", sa.String(128), nullable=True),
        sa.Column("os_version", sa.String(64), nullable=True),
        sa.Column("kernel", sa.String(128), nullable=True),
        sa.Column("arch", sa.String(32), nullable=True),
        sa.Column("cpu_model", sa.String(255), nullable=True),
        sa.Column("cpu_cores", sa.Integer(), nullable=True),
        sa.Column("total_memory_bytes", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "device_key",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "device_id", sa.Uuid(), sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
    )
    # At most one live key per device, enforced by the database.
    op.create_index(
        "uq_device_key_active",
        "device_key",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "device_resources",
        sa.Column(
            "device_id",
            sa.Uuid(),
            sa.ForeignKey("device.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sampled_at", TS, nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("memory_total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("memory_available_bytes", sa.BigInteger(), nullable=True),
        sa.Column("load_1", sa.Float(), nullable=True),
        sa.Column("load_5", sa.Float(), nullable=True),
        sa.Column("load_15", sa.Float(), nullable=True),
        sa.Column("uptime_seconds", sa.BigInteger(), nullable=True),
        sa.Column(
            "disks",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.create_table(
        "package",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ecosystem", ECOSYSTEM, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
    )
    op.create_index(
        "uq_package_identity", "package", ["ecosystem", "normalized_name"], unique=True
    )

    op.create_table(
        "inventory_report",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "device_id", sa.Uuid(), sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("collected_at", TS, nullable=False),
        sa.Column("received_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("package_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("change_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "collector_status",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("superseded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_inventory_report_device_collected",
        "inventory_report",
        ["device_id", "collected_at"],
    )

    op.create_table(
        "device_package",
        sa.Column(
            "device_id",
            sa.Uuid(),
            sa.ForeignKey("device.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "package_id",
            sa.Integer(),
            sa.ForeignKey("package.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Part of the key: one device can hold the same package at different
        # versions under two interpreters.
        sa.Column("scope", sa.String(255), primary_key=True),
        sa.Column("version", sa.String(255), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )

    op.create_table(
        "package_change",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "device_id", sa.Uuid(), sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "package_id",
            sa.Integer(),
            sa.ForeignKey("package.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("change_type", CHANGE_TYPE, nullable=False),
        sa.Column("old_version", sa.String(255), nullable=True),
        sa.Column("new_version", sa.String(255), nullable=True),
        sa.Column("detected_at", TS, nullable=False),
        sa.Column(
            "report_id",
            sa.BigInteger(),
            sa.ForeignKey("inventory_report.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_package_change_device_detected", "package_change", ["device_id", "detected_at"]
    )

    op.create_table(
        "task",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "device_id", sa.Uuid(), sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", TASK_STATUS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("claimed_at", TS, nullable=True),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(2000), nullable=True),
    )
    op.create_index("ix_task_device_status", "task", ["device_id", "status"])

    op.create_table(
        "agent_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "device_id", sa.Uuid(), sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column(
            "inventory_interval_seconds", sa.Integer(), nullable=False, server_default="43200"
        ),
        sa.Column("spool_max_bytes", sa.BigInteger(), nullable=False, server_default="67108864"),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("poll_interval_seconds BETWEEN 30 AND 3600", name="ck_poll_interval"),
        sa.CheckConstraint(
            "inventory_interval_seconds BETWEEN 300 AND 604800", name="ck_inventory_interval"
        ),
        sa.CheckConstraint(
            "spool_max_bytes BETWEEN 1048576 AND 10737418240", name="ck_spool_cap"
        ),
    )
    op.create_index(
        "uq_agent_config_device",
        "agent_config",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("device_id IS NOT NULL"),
    )

    # Seed the global config row so agents have defaults before any admin
    # visits the settings page.
    op.execute(
        "INSERT INTO agent_config (id, device_id) VALUES (1, NULL) ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("agent_config")
    op.drop_table("task")
    op.drop_table("package_change")
    op.drop_table("device_package")
    op.drop_table("inventory_report")
    op.drop_table("package")
    op.drop_table("device_resources")
    op.drop_table("device_key")
    op.drop_table("device")
    op.drop_table("admin_session")
    op.drop_table("admin_user")

    bind = op.get_bind()
    TASK_STATUS.drop(bind, checkfirst=True)
    CHANGE_TYPE.drop(bind, checkfirst=True)
    ECOSYSTEM.drop(bind, checkfirst=True)
