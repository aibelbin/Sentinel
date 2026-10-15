"""Agent self-update: release tracking and per-device update outcome.

Revision ID: 0002_agent_updates
Revises: 0001_initial
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_agent_updates"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "agent_release",
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("quarantined_at", TS, nullable=True),
        sa.Column("failure_reason", sa.String(1000), nullable=True),
        # SET NULL rather than CASCADE: deleting the device that reported a bad
        # release must not erase the record that the release is bad.
        sa.Column(
            "failure_device_id",
            sa.Uuid(),
            sa.ForeignKey("device.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("retry_requested_at", TS, nullable=True),
    )
    op.create_index("ix_agent_release_failure_device", "agent_release", ["failure_device_id"])

    op.add_column("device", sa.Column("previous_agent_version", sa.String(64), nullable=True))
    op.add_column("device", sa.Column("last_update_at", TS, nullable=True))
    op.add_column("device", sa.Column("last_update_result", sa.String(32), nullable=True))
    op.add_column("device", sa.Column("last_update_detail", sa.String(500), nullable=True))

    # agent_version now holds a source hash, which is longer than the release
    # string it used to hold.
    op.alter_column(
        "device",
        "agent_version",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "device",
        "agent_version",
        existing_type=sa.String(64),
        type_=sa.String(32),
        existing_nullable=True,
    )
    op.drop_column("device", "last_update_detail")
    op.drop_column("device", "last_update_result")
    op.drop_column("device", "last_update_at")
    op.drop_column("device", "previous_agent_version")
    op.drop_index("ix_agent_release_failure_device", table_name="agent_release")
    op.drop_table("agent_release")
