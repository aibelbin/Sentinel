"""Rename the client "agent" to "watchdog".

Revision ID: 0003_rename_agent_to_watchdog
Revises: 0002_agent_updates
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_rename_agent_to_watchdog"
down_revision: str | None = "0002_agent_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename(old: str, new: str) -> None:
    op.execute(f"ALTER TABLE {old}_config RENAME TO {new}_config")
    op.execute(f"ALTER TABLE {old}_release RENAME TO {new}_release")
    op.execute(f"ALTER TABLE {new}_config RENAME CONSTRAINT {old}_config_pkey TO {new}_config_pkey")
    op.execute(f"ALTER TABLE {new}_release RENAME CONSTRAINT {old}_release_pkey TO {new}_release_pkey")
    op.execute(f"ALTER SEQUENCE {old}_config_id_seq RENAME TO {new}_config_id_seq")
    op.execute(f"ALTER INDEX uq_{old}_config_device RENAME TO uq_{new}_config_device")
    op.execute(f"ALTER INDEX ix_{old}_release_failure_device RENAME TO ix_{new}_release_failure_device")
    op.execute(f"ALTER TABLE device RENAME COLUMN {old}_version TO {new}_version")
    op.execute(
        f"ALTER TABLE device RENAME COLUMN previous_{old}_version TO previous_{new}_version"
    )
    # Queued or historical update jobs carry the old type string.
    op.execute(f"UPDATE task SET type = 'update_{new}' WHERE type = 'update_{old}'")


def upgrade() -> None:
    _rename("agent", "watchdog")


def downgrade() -> None:
    _rename("watchdog", "agent")
