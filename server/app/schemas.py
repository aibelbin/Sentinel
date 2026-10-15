from datetime import datetime
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")

AGENT_VERSION_MAX = 64
MAX_PACKAGES_PER_REPORT = 20000


# --------------------------------------------------------------------------- #
# Admin auth
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    username: str


# --------------------------------------------------------------------------- #
# Shared
# --------------------------------------------------------------------------- #


class AgentConfigOut(BaseModel):
    poll_interval_seconds: int
    inventory_interval_seconds: int
    spool_max_bytes: int


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class Disk(BaseModel):
    mountpoint: str = Field(max_length=255)
    filesystem: str = Field(max_length=64)
    total_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)


class HostFacts(BaseModel):
    hostname: str | None = Field(default=None, max_length=255)
    os_name: str | None = Field(default=None, max_length=128)
    os_version: str | None = Field(default=None, max_length=64)
    kernel: str | None = Field(default=None, max_length=128)
    arch: str | None = Field(default=None, max_length=32)
    cpu_model: str | None = Field(default=None, max_length=255)
    cpu_cores: int | None = Field(default=None, ge=0, le=4096)
    total_memory_bytes: int | None = Field(default=None, ge=0)


# --------------------------------------------------------------------------- #
# Agent routes
# --------------------------------------------------------------------------- #


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version: str = Field(max_length=AGENT_VERSION_MAX)
    host: HostFacts


class EnrollResponse(BaseModel):
    device_id: UUID
    name: str
    config: AgentConfigOut


class ResourceSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null on the first beat after enrollment: CPU percentage needs a prior
    # /proc/stat sample to difference against.
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    load_1: float | None = Field(default=None, ge=0)
    load_5: float | None = Field(default=None, ge=0)
    load_15: float | None = Field(default=None, ge=0)
    uptime_seconds: int | None = Field(default=None, ge=0)
    disks: list[Disk] = Field(default_factory=list, max_length=64)


class UpdateReport(BaseModel):
    """How the machine's last agent update went.

    A revert is only observable from here: the server handed out a version, the
    machine tried it and went back, and nothing else would tell it so.
    """

    model_config = ConfigDict(extra="forbid")

    result: Literal["ok", "installed", "reverted", "failed"] | None = None
    detail: str | None = Field(default=None, max_length=500)
    at: datetime | None = None
    version: str | None = Field(default=None, max_length=64)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version: str = Field(max_length=AGENT_VERSION_MAX)
    spool_depth: int = Field(default=0, ge=0)
    resources: ResourceSample = Field(default_factory=ResourceSample)
    host: HostFacts | None = None
    update: UpdateReport | None = None


class TaskOut(BaseModel):
    id: UUID
    type: str
    params: dict


class HeartbeatResponse(BaseModel):
    config: AgentConfigOut
    tasks: list[TaskOut]
    server_time: datetime


class PackageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ecosystem: Literal["os", "python", "npm", "java"]
    name: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=255)
    scope: str = Field(default="system", min_length=1, max_length=255)
    meta: dict = Field(default_factory=dict)


class InventoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collected_at: datetime
    agent_version: str = Field(max_length=AGENT_VERSION_MAX)
    collector_status: dict[str, str] = Field(default_factory=dict)
    packages: list[PackageIn] = Field(max_length=MAX_PACKAGES_PER_REPORT)
    host: HostFacts | None = None


class InventoryResponse(BaseModel):
    report_id: int
    change_count: int
    package_count: int
    superseded: bool


class TaskResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    result: dict | None = None
    error: str | None = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------- #
# Admin: devices
# --------------------------------------------------------------------------- #


def _require_non_blank(value: str | None) -> str | None:
    """Strip before length validation.

    Without this, min_length=1 passes for "   " and the route's own .strip()
    then stores an empty name, which renders as a blank row and makes the
    delete confirmation unsatisfiable.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)

    _strip_name = field_validator("name")(_require_non_blank)


class DeviceKeyIssued(BaseModel):
    """The plaintext key is returned exactly once, at creation or rotation."""

    device_id: UUID
    name: str
    api_key: str


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None

    _strip_name = field_validator("name")(_require_non_blank)


class ResourcesOut(BaseModel):
    sampled_at: datetime
    cpu_percent: float | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    load_1: float | None
    load_5: float | None
    load_15: float | None
    uptime_seconds: int | None
    disks: list[Disk]


class AgentBundle(BaseModel):
    """The agent code an updating machine downloads."""

    version: str
    sha256: str
    bundle_b64: str


class ReleaseOut(BaseModel):
    version: str
    sha256: str
    first_seen_at: datetime
    quarantined: bool
    failure_reason: str | None
    devices_on_version: int
    devices_total: int


class DeviceSummary(BaseModel):
    id: UUID
    name: str
    status: str
    enabled: bool
    created_at: datetime
    last_seen_at: datetime | None
    agent_version: str | None
    hostname: str | None
    os_name: str | None
    os_version: str | None
    package_count: int
    resources: ResourcesOut | None
    update_status: str


class DeviceDetail(DeviceSummary):
    kernel: str | None
    arch: str | None
    cpu_model: str | None
    cpu_cores: int | None
    total_memory_bytes: int | None
    config: AgentConfigOut
    last_report_at: datetime | None
    last_collector_status: dict[str, str]
    previous_agent_version: str | None
    last_update_at: datetime | None
    last_update_result: str | None
    last_update_detail: str | None
    target_agent_version: str | None


class PackageOut(BaseModel):
    ecosystem: str
    name: str
    version: str
    scope: str
    first_seen_at: datetime
    last_seen_at: datetime


class ChangeOut(BaseModel):
    id: int
    ecosystem: str
    name: str
    scope: str
    change_type: str
    old_version: str | None
    new_version: str | None
    detected_at: datetime


class ReportOut(BaseModel):
    id: int
    collected_at: datetime
    received_at: datetime
    package_count: int
    change_count: int
    superseded: bool
    collector_status: dict[str, str]


class TaskHistoryOut(BaseModel):
    id: UUID
    type: str
    status: str
    created_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    result: dict | None
    error: str | None
    abandoned: bool


class TaskCreate(BaseModel):
    type: Literal["collect_inventory"] = "collect_inventory"
    params: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Admin: config and installer
# --------------------------------------------------------------------------- #


class ConfigUpdate(BaseModel):
    poll_interval_seconds: int = Field(ge=30, le=3600)
    inventory_interval_seconds: int = Field(ge=300, le=604800)
    spool_max_bytes: int = Field(ge=1048576, le=10737418240)


class InstallerRequest(BaseModel):
    """Values baked into the downloaded installer.

    Sent in a POST body rather than a query string so the API key does not land
    in access logs.
    """

    model_config = ConfigDict(extra="forbid")

    server_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=256)
    insecure: bool = False
