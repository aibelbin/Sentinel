import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Ecosystem(str, enum.Enum):
    os = "os"
    python = "python"
    npm = "npm"
    java = "java"


class ChangeType(str, enum.Enum):
    added = "added"
    removed = "removed"
    version_changed = "version_changed"


class TaskStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


def _ts(**kw) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class AdminUser(Base):
    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = _ts(nullable=True)


class AdminSession(Base):
    __tablename__ = "admin_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_user.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = _ts(nullable=False)
    last_used_at: Mapped[datetime | None] = _ts(nullable=True)


class Device(Base):
    __tablename__ = "device"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    last_seen_at: Mapped[datetime | None] = _ts(nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Host facts are metadata, never identity. Devices are never matched on them.
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kernel: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arch: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cpu_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_memory_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # How this machine's last agent update went. Reported by the agent, because
    # a machine reverting itself is something only the machine knows.
    previous_agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_update_at: Mapped[datetime | None] = _ts(nullable=True)
    last_update_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_update_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    keys: Mapped[list["DeviceKey"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    resources: Mapped["DeviceResources | None"] = relationship(
        back_populates="device", cascade="all, delete-orphan", uselist=False
    )


class DeviceKey(Base):
    __tablename__ = "device_key"
    __table_args__ = (
        # At most one live key per device, enforced by the database rather than
        # by application logic alone.
        Index(
            "uq_device_key_active",
            "device_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)
    last_used_at: Mapped[datetime | None] = _ts(nullable=True)

    device: Mapped[Device] = relationship(back_populates="keys")


class DeviceResources(Base):
    """Latest heartbeat sample only. Overwritten every beat.

    Kept in its own table so the frequently-written row is separate from the
    rarely-written device row, and so a time-series table can be added later
    without touching this one.
    """

    __tablename__ = "device_resources"

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), primary_key=True
    )
    sampled_at: Mapped[datetime] = _ts(nullable=False)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    memory_available_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    load_1: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_5: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_15: Mapped[float | None] = mapped_column(Float, nullable=True)
    uptime_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disks: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    device: Mapped[Device] = relationship(back_populates="resources")


class Package(Base):
    """Shared catalogue, so a package on fifty devices is one row."""

    __tablename__ = "package"
    __table_args__ = (
        Index("uq_package_identity", "ecosystem", "normalized_name", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ecosystem: Mapped[Ecosystem] = mapped_column(
        SAEnum(Ecosystem, name="ecosystem"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)


class DevicePackage(Base):
    """Current state. `scope` is part of the key because one device can hold the
    same package at different versions under two interpreters."""

    __tablename__ = "device_package"
    __table_args__ = (Index("ix_device_package_package", "package_id"),)

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), primary_key=True
    )
    package_id: Mapped[int] = mapped_column(
        ForeignKey("package.id", ondelete="CASCADE"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    version: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen_at: Mapped[datetime] = _ts(nullable=False)
    last_seen_at: Mapped[datetime] = _ts(nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    package: Mapped[Package] = relationship(lazy="joined")


class InventoryReport(Base):
    __tablename__ = "inventory_report"
    __table_args__ = (
        Index("ix_inventory_report_device_collected", "device_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False
    )
    collected_at: Mapped[datetime] = _ts(nullable=False)
    received_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)
    package_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    collector_status: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # True when the report arrived after a newer one had already been applied.
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class PackageChange(Base):
    __tablename__ = "package_change"
    __table_args__ = (
        Index("ix_package_change_device_detected", "device_id", "detected_at"),
        Index("ix_package_change_report", "report_id"),
        Index("ix_package_change_package", "package_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False
    )
    package_id: Mapped[int] = mapped_column(
        ForeignKey("package.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    change_type: Mapped[ChangeType] = mapped_column(
        SAEnum(ChangeType, name="change_type"), nullable=False
    )
    old_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detected_at: Mapped[datetime] = _ts(nullable=False)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_report.id", ondelete="CASCADE"), nullable=False
    )

    package: Mapped[Package] = relationship(lazy="joined")


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (Index("ix_task_device_status", "device_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, name="task_status"),
        nullable=False,
        default=TaskStatus.pending,
    )
    created_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)
    claimed_at: Mapped[datetime | None] = _ts(nullable=True)
    completed_at: Mapped[datetime | None] = _ts(nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class AgentConfig(Base):
    """Row with device_id NULL (id = 1) is the global default; rows with a
    device_id are per-device overrides."""

    __tablename__ = "agent_config"
    __table_args__ = (
        Index(
            "uq_agent_config_device",
            "device_id",
            unique=True,
            postgresql_where=text("device_id IS NOT NULL"),
        ),
        CheckConstraint("poll_interval_seconds BETWEEN 30 AND 3600", name="ck_poll_interval"),
        CheckConstraint(
            "inventory_interval_seconds BETWEEN 300 AND 604800", name="ck_inventory_interval"
        ),
        CheckConstraint("spool_max_bytes BETWEEN 1048576 AND 10737418240", name="ck_spool_cap"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=True
    )
    poll_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="60"
    )
    inventory_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="43200"
    )
    spool_max_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="67108864"
    )
    updated_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)


class AgentRelease(Base):
    """One row per agent version the server has served.

    The version is a hash of the agent source, so rows accumulate as the code
    changes and a quarantine naturally applies to exactly one build.
    """

    __tablename__ = "agent_release"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = _ts(server_default=func.now(), nullable=False)

    # Set when a machine reports this version failed to install or run. While
    # set, the server stops handing the version out to anyone else.
    quarantined_at: Mapped[datetime | None] = _ts(nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    failure_device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL"), nullable=True
    )
    # Set when an operator clears a quarantine. Failed attempts from before
    # this moment are ignored, so a retry is a genuinely fresh attempt without
    # deleting the record of what failed.
    retry_requested_at: Mapped[datetime | None] = _ts(nullable=True)


GLOBAL_CONFIG_ID = 1
