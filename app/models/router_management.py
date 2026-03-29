import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RouterStatus(enum.Enum):
    online = "online"
    offline = "offline"
    degraded = "degraded"
    maintenance = "maintenance"
    unreachable = "unreachable"


class RouterAccessMethod(enum.Enum):
    direct = "direct"
    jump_host = "jump_host"


class RouterSnapshotSource(enum.Enum):
    manual = "manual"
    scheduled = "scheduled"
    pre_change = "pre_change"
    post_change = "post_change"


class RouterTemplateCategory(enum.Enum):
    firewall = "firewall"
    queue = "queue"
    address_list = "address_list"
    routing = "routing"
    dns = "dns"
    ntp = "ntp"
    snmp = "snmp"
    system = "system"
    custom = "custom"


class RouterConfigPushStatus(enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    partial_failure = "partial_failure"
    failed = "failed"
    rolled_back = "rolled_back"


class RouterPushResultStatus(enum.Enum):
    pending = "pending"
    success = "success"
    failed = "failed"
    skipped = "skipped"


class JumpHost(Base):
    __tablename__ = "jump_hosts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_password: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    routers: Mapped[list["Router"]] = relationship(back_populates="jump_host")


class Router(Base):
    __tablename__ = "routers"
    __table_args__ = (
        Index("ix_routers_status", "status"),
        Index("ix_routers_management_ip", "management_ip"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    management_ip: Mapped[str] = mapped_column(String(255), nullable=False)
    rest_api_port: Mapped[int] = mapped_column(Integer, default=443)
    rest_api_username: Mapped[str] = mapped_column(String(255), nullable=False)
    rest_api_password: Mapped[str] = mapped_column(String(512), nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=False)

    routeros_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    board_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    architecture: Mapped[str | None] = mapped_column(String(50), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    firmware_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    access_method: Mapped[RouterAccessMethod] = mapped_column(
        Enum(RouterAccessMethod, name="routeraccessmethod", create_constraint=False),
        default=RouterAccessMethod.direct,
    )
    jump_host_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jump_hosts.id"), nullable=True
    )
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id"), nullable=True
    )
    network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=True
    )

    status: Mapped[RouterStatus] = mapped_column(
        Enum(RouterStatus, name="routerstatus", create_constraint=False),
        default=RouterStatus.offline,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_config_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_config_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    jump_host: Mapped[JumpHost | None] = relationship(back_populates="routers")
    interfaces: Mapped[list["RouterInterface"]] = relationship(
        back_populates="router", cascade="all, delete-orphan"
    )
    config_snapshots: Mapped[list["RouterConfigSnapshot"]] = relationship(
        back_populates="router", cascade="all, delete-orphan"
    )


class RouterInterface(Base):
    __tablename__ = "router_interfaces"
    __table_args__ = (
        UniqueConstraint("router_id", "name", name="uq_router_interface_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    router_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("routers.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False, default="ether")
    mac_address: Mapped[str | None] = mapped_column(String(17), nullable=True)
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rx_byte: Mapped[int] = mapped_column(BigInteger, default=0)
    tx_byte: Mapped[int] = mapped_column(BigInteger, default=0)
    rx_packet: Mapped[int] = mapped_column(BigInteger, default=0)
    tx_packet: Mapped[int] = mapped_column(BigInteger, default=0)
    last_link_up_time: Mapped[str | None] = mapped_column(String(100), nullable=True)
    speed: Mapped[str | None] = mapped_column(String(50), nullable=True)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    router: Mapped[Router] = relationship(back_populates="interfaces")


class RouterConfigSnapshot(Base):
    __tablename__ = "router_config_snapshots"
    __table_args__ = (
        Index("ix_router_config_snapshots_router_id", "router_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    router_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("routers.id", ondelete="CASCADE"),
        nullable=False,
    )
    config_export: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[RouterSnapshotSource] = mapped_column(
        Enum(
            RouterSnapshotSource, name="routersnapshotsource", create_constraint=False
        ),
        nullable=False,
    )
    captured_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    router: Mapped[Router] = relationship(back_populates="config_snapshots")


class RouterConfigTemplate(Base):
    __tablename__ = "router_config_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[RouterTemplateCategory] = mapped_column(
        Enum(
            RouterTemplateCategory,
            name="routertemplatecategory",
            create_constraint=False,
        ),
        default=RouterTemplateCategory.custom,
    )
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RouterConfigPush(Base):
    __tablename__ = "router_config_pushes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("router_config_templates.id"),
        nullable=True,
    )
    commands: Mapped[list] = mapped_column(JSON, nullable=False)
    variable_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    initiated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[RouterConfigPushStatus] = mapped_column(
        Enum(
            RouterConfigPushStatus,
            name="routerconfigpushstatus",
            create_constraint=False,
        ),
        default=RouterConfigPushStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    template: Mapped[RouterConfigTemplate | None] = relationship()
    results: Mapped[list["RouterConfigPushResult"]] = relationship(
        back_populates="push", cascade="all, delete-orphan"
    )


class RouterConfigPushResult(Base):
    __tablename__ = "router_config_push_results"
    __table_args__ = (Index("ix_push_results_push_id", "push_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    push_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("router_config_pushes.id", ondelete="CASCADE"),
        nullable=False,
    )
    router_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routers.id"), nullable=False
    )
    status: Mapped[RouterPushResultStatus] = mapped_column(
        Enum(
            RouterPushResultStatus,
            name="routerpushresultstatus",
            create_constraint=False,
        ),
        default=RouterPushResultStatus.pending,
    )
    response_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    pre_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("router_config_snapshots.id"),
        nullable=True,
    )
    post_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("router_config_snapshots.id"),
        nullable=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    push: Mapped[RouterConfigPush] = relationship(back_populates="results")
    router: Mapped[Router] = relationship()
    pre_snapshot: Mapped[RouterConfigSnapshot | None] = relationship(
        foreign_keys=[pre_snapshot_id]
    )
    post_snapshot: Mapped[RouterConfigSnapshot | None] = relationship(
        foreign_keys=[post_snapshot_id]
    )
