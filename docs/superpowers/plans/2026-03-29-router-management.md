# Router Management Module — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add centralized MikroTik router config management to dotmac_sub — inventory, bulk config push, config snapshots, live health, and admin UI.

**Architecture:** New `router_management` module within dotmac_sub. Router model links to existing NasDevice (RADIUS) and NetworkDevice (monitoring) via optional FKs. RouterOS REST API communication via httpx with SSH tunnel support for jump-host access. Celery tasks for periodic sync. Admin UI follows existing HTMX + Alpine.js + Tailwind patterns.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, httpx, sshtunnel, Celery, Jinja2/HTMX/Alpine.js/Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-29-router-management-design.md`

---

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `app/models/router_management.py` | All enums and models: Router, JumpHost, RouterInterface, RouterConfigSnapshot, RouterConfigTemplate, RouterConfigPush, RouterConfigPushResult |
| `app/schemas/router_management.py` | Pydantic request/response schemas for all router management endpoints |
| `app/services/router_management/connection.py` | RouterConnectionService — httpx client creation, SSH tunnel management, REST API execution |
| `app/services/router_management/inventory.py` | RouterInventoryService — Router + JumpHost CRUD, system info sync, interface sync |
| `app/services/router_management/config.py` | RouterConfigService — snapshots, templates CRUD, config push execution, command blocklist |
| `app/services/router_management/monitoring.py` | RouterMonitoringService — dashboard summary, live health, NetworkDevice linking |
| `app/services/router_management/__init__.py` | Package init |
| `app/api/router_management.py` | All REST API endpoints for router management |
| `app/web/admin/network_routers.py` | Admin web routes for router management UI |
| `app/tasks/router_sync.py` | Celery tasks: periodic sync, snapshot capture, tunnel cleanup |
| `templates/admin/network/routers/index.html` | Router list page |
| `templates/admin/network/routers/dashboard.html` | Router dashboard page |
| `templates/admin/network/routers/detail.html` | Router detail page (tabbed) |
| `templates/admin/network/routers/form.html` | Router create/edit form |
| `templates/admin/network/routers/templates/index.html` | Config template list |
| `templates/admin/network/routers/templates/form.html` | Config template editor |
| `templates/admin/network/routers/templates/detail.html` | Config template detail + preview |
| `templates/admin/network/routers/push.html` | Bulk push wizard |
| `templates/admin/network/routers/push_detail.html` | Push results page |
| `templates/admin/network/routers/jump_hosts.html` | Jump host management |
| `alembic/versions/005_router_management.py` | Database migration |
| `tests/test_router_management_models.py` | Model tests |
| `tests/test_router_management_services.py` | Service tests |
| `tests/test_router_management_api.py` | API endpoint tests |

### Modified Files

| File | Change |
|------|--------|
| `app/models/__init__.py` | Export new models and enums |
| `app/models/network_operation.py` | Add new NetworkOperationType enum values |
| `app/main.py` | Register new API and web routers |
| `app/config.py` | Add router management settings |

---

## Task 1: Models and Migration

**Files:**
- Create: `app/models/router_management.py`
- Modify: `app/models/__init__.py`
- Modify: `app/models/network_operation.py`
- Create: `alembic/versions/005_router_management.py`
- Create: `tests/test_router_management_models.py`

- [ ] **Step 1: Write model tests**

Create `tests/test_router_management_models.py`:

```python
import uuid

from app.models.router_management import (
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterInterface,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterStatus,
    RouterTemplateCategory,
)


def test_router_creation(db_session):
    router = Router(
        name="router-hq",
        hostname="hq-core",
        management_ip="10.0.0.1",
        rest_api_port=443,
        rest_api_username="admin",
        rest_api_password="enc:test",
        access_method=RouterAccessMethod.direct,
        status=RouterStatus.online,
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    assert router.id is not None
    assert router.name == "router-hq"
    assert router.status == RouterStatus.online
    assert router.access_method == RouterAccessMethod.direct
    assert router.use_ssl is True
    assert router.verify_tls is False
    assert router.is_active is True


def test_jump_host_creation(db_session):
    jh = JumpHost(
        name="jump-dc1",
        hostname="jump.example.com",
        port=22,
        username="tunnel",
        ssh_key="enc:testkey",
    )
    db_session.add(jh)
    db_session.commit()
    db_session.refresh(jh)

    assert jh.id is not None
    assert jh.name == "jump-dc1"
    assert jh.is_active is True


def test_router_with_jump_host(db_session):
    jh = JumpHost(
        name="jump-dc2",
        hostname="jump2.example.com",
        username="tunnel",
    )
    db_session.add(jh)
    db_session.commit()
    db_session.refresh(jh)

    router = Router(
        name="router-remote",
        hostname="remote-1",
        management_ip="192.168.1.1",
        rest_api_username="admin",
        rest_api_password="enc:test",
        access_method=RouterAccessMethod.jump_host,
        jump_host_id=jh.id,
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    assert router.jump_host_id == jh.id
    assert router.jump_host.name == "jump-dc2"


def test_router_interface(db_session):
    router = Router(
        name="router-iface-test",
        hostname="test-1",
        management_ip="10.0.0.2",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    iface = RouterInterface(
        router_id=router.id,
        name="ether1",
        type="ether",
        mac_address="AA:BB:CC:DD:EE:FF",
        is_running=True,
        is_disabled=False,
    )
    db_session.add(iface)
    db_session.commit()
    db_session.refresh(iface)

    assert iface.router_id == router.id
    assert iface.name == "ether1"
    assert iface.is_running is True


def test_config_snapshot(db_session):
    router = Router(
        name="router-snap-test",
        hostname="snap-1",
        management_ip="10.0.0.3",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    snap = RouterConfigSnapshot(
        router_id=router.id,
        config_export="/ip address\nadd address=10.0.0.1/24 interface=ether1",
        config_hash="abc123",
        source=RouterSnapshotSource.manual,
    )
    db_session.add(snap)
    db_session.commit()
    db_session.refresh(snap)

    assert snap.router_id == router.id
    assert snap.source == RouterSnapshotSource.manual


def test_config_template(db_session):
    tmpl = RouterConfigTemplate(
        name="sfq-queues",
        description="Set SFQ on all queues",
        template_body="/queue simple set [find] queue=sfq/sfq",
        category=RouterTemplateCategory.queue,
        variables={},
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)

    assert tmpl.name == "sfq-queues"
    assert tmpl.category == RouterTemplateCategory.queue
    assert tmpl.is_active is True


def test_config_push_with_results(db_session):
    router = Router(
        name="router-push-test",
        hostname="push-1",
        management_ip="10.0.0.4",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    push = RouterConfigPush(
        commands=["/queue simple set [find] queue=sfq/sfq"],
        initiated_by=uuid.uuid4(),
        status=RouterConfigPushStatus.pending,
    )
    db_session.add(push)
    db_session.commit()
    db_session.refresh(push)

    result = RouterConfigPushResult(
        push_id=push.id,
        router_id=router.id,
        status=RouterPushResultStatus.pending,
    )
    db_session.add(result)
    db_session.commit()
    db_session.refresh(result)

    assert result.push_id == push.id
    assert result.router_id == router.id
    assert result.status == RouterPushResultStatus.pending
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.router_management'`

- [ ] **Step 3: Create the models file**

Create `app/models/router_management.py`:

```python
import enum
import uuid
from datetime import datetime, timezone

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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
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
        UUID(as_uuid=True), ForeignKey("routers.id", ondelete="CASCADE"), nullable=False
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
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
        UUID(as_uuid=True), ForeignKey("routers.id", ondelete="CASCADE"), nullable=False
    )
    config_export: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[RouterSnapshotSource] = mapped_column(
        Enum(RouterSnapshotSource, name="routersnapshotsource", create_constraint=False),
        nullable=False,
    )
    captured_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
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
        Enum(RouterTemplateCategory, name="routertemplatecategory", create_constraint=False),
        default=RouterTemplateCategory.custom,
    )
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
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
    initiated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    status: Mapped[RouterConfigPushStatus] = mapped_column(
        Enum(RouterConfigPushStatus, name="routerconfigpushstatus", create_constraint=False),
        default=RouterConfigPushStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
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
    __table_args__ = (
        Index("ix_push_results_push_id", "push_id"),
    )

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
        Enum(RouterPushResultStatus, name="routerpushresultstatus", create_constraint=False),
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    push: Mapped[RouterConfigPush] = relationship(back_populates="results")
    router: Mapped[Router] = relationship()
    pre_snapshot: Mapped[RouterConfigSnapshot | None] = relationship(
        foreign_keys=[pre_snapshot_id]
    )
    post_snapshot: Mapped[RouterConfigSnapshot | None] = relationship(
        foreign_keys=[post_snapshot_id]
    )
```

- [ ] **Step 4: Add exports to models __init__.py**

Add to `app/models/__init__.py`:

```python
from app.models.router_management import (  # noqa: F401
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterInterface,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterStatus,
    RouterTemplateCategory,
)
```

- [ ] **Step 5: Add NetworkOperationType values**

In `app/models/network_operation.py`, add to the `NetworkOperationType` enum:

```python
    router_config_push = "router_config_push"
    router_config_backup = "router_config_backup"
    router_reboot = "router_reboot"
    router_firmware_upgrade = "router_firmware_upgrade"
    router_bulk_push = "router_bulk_push"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_models.py -v`
Expected: All 7 tests PASS

- [ ] **Step 7: Create Alembic migration**

Create `alembic/versions/005_router_management.py`:

```python
"""router management tables

Revision ID: 005_router_management
Revises: <previous_revision>
Create Date: 2026-03-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSON, UUID

revision = "005_router_management"
down_revision = None  # Set to actual previous revision
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return name in insp.get_table_names()


def _add_enum_value_if_not_exists(enum_name: str, value: str) -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_enum WHERE enumlabel = :val "
            "AND enumtypid = (SELECT oid FROM pg_type WHERE typname = :name)"
        ),
        {"val": value, "name": enum_name},
    )
    if result.fetchone() is None:
        conn.execute(
            sa.text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS :val"),
            {"val": value},
        )


def upgrade() -> None:
    if not _table_exists("jump_hosts"):
        op.create_table(
            "jump_hosts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), unique=True, nullable=False),
            sa.Column("hostname", sa.String(255), nullable=False),
            sa.Column("port", sa.Integer, server_default="22"),
            sa.Column("username", sa.String(255), nullable=False),
            sa.Column("ssh_key", sa.Text, nullable=True),
            sa.Column("ssh_password", sa.String(512), nullable=True),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _table_exists("routers"):
        op.create_table(
            "routers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), unique=True, nullable=False),
            sa.Column("hostname", sa.String(255), nullable=False),
            sa.Column("management_ip", sa.String(255), nullable=False),
            sa.Column("rest_api_port", sa.Integer, server_default="443"),
            sa.Column("rest_api_username", sa.String(255), nullable=False),
            sa.Column("rest_api_password", sa.String(512), nullable=False),
            sa.Column("use_ssl", sa.Boolean, server_default="true"),
            sa.Column("verify_tls", sa.Boolean, server_default="false"),
            sa.Column("routeros_version", sa.String(50), nullable=True),
            sa.Column("board_name", sa.String(100), nullable=True),
            sa.Column("architecture", sa.String(50), nullable=True),
            sa.Column("serial_number", sa.String(100), nullable=True),
            sa.Column("firmware_type", sa.String(50), nullable=True),
            sa.Column("location", sa.String(255), nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("tags", JSON, nullable=True),
            sa.Column("access_method", sa.String(20), server_default="direct"),
            sa.Column("jump_host_id", UUID(as_uuid=True), sa.ForeignKey("jump_hosts.id"), nullable=True),
            sa.Column("nas_device_id", UUID(as_uuid=True), sa.ForeignKey("nas_devices.id"), nullable=True),
            sa.Column("network_device_id", UUID(as_uuid=True), sa.ForeignKey("network_devices.id"), nullable=True),
            sa.Column("status", sa.String(20), server_default="offline"),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_config_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_config_change_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reseller_id", UUID(as_uuid=True), nullable=True),
            sa.Column("organization_id", UUID(as_uuid=True), nullable=True),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_routers_status", "routers", ["status"])
        op.create_index("ix_routers_management_ip", "routers", ["management_ip"])

    if not _table_exists("router_interfaces"):
        op.create_table(
            "router_interfaces",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("router_id", UUID(as_uuid=True), sa.ForeignKey("routers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("type", sa.String(50), server_default="ether"),
            sa.Column("mac_address", sa.String(17), nullable=True),
            sa.Column("is_running", sa.Boolean, server_default="false"),
            sa.Column("is_disabled", sa.Boolean, server_default="false"),
            sa.Column("rx_byte", sa.BigInteger, server_default="0"),
            sa.Column("tx_byte", sa.BigInteger, server_default="0"),
            sa.Column("rx_packet", sa.BigInteger, server_default="0"),
            sa.Column("tx_packet", sa.BigInteger, server_default="0"),
            sa.Column("last_link_up_time", sa.String(100), nullable=True),
            sa.Column("speed", sa.String(50), nullable=True),
            sa.Column("comment", sa.String(255), nullable=True),
            sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_unique_constraint("uq_router_interface_name", "router_interfaces", ["router_id", "name"])

    if not _table_exists("router_config_snapshots"):
        op.create_table(
            "router_config_snapshots",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("router_id", UUID(as_uuid=True), sa.ForeignKey("routers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("config_export", sa.Text, nullable=False),
            sa.Column("config_hash", sa.String(64), nullable=False),
            sa.Column("source", sa.String(20), nullable=False),
            sa.Column("captured_by", UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_router_config_snapshots_router_id", "router_config_snapshots", ["router_id"])

    if not _table_exists("router_config_templates"):
        op.create_table(
            "router_config_templates",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), unique=True, nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("template_body", sa.Text, nullable=False),
            sa.Column("category", sa.String(20), server_default="custom"),
            sa.Column("variables", JSON, server_default="{}"),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _table_exists("router_config_pushes"):
        op.create_table(
            "router_config_pushes",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("template_id", UUID(as_uuid=True), sa.ForeignKey("router_config_templates.id"), nullable=True),
            sa.Column("commands", JSON, nullable=False),
            sa.Column("variable_values", JSON, nullable=True),
            sa.Column("initiated_by", UUID(as_uuid=True), nullable=False),
            sa.Column("status", sa.String(20), server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _table_exists("router_config_push_results"):
        op.create_table(
            "router_config_push_results",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("push_id", UUID(as_uuid=True), sa.ForeignKey("router_config_pushes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("router_id", UUID(as_uuid=True), sa.ForeignKey("routers.id"), nullable=False),
            sa.Column("status", sa.String(20), server_default="pending"),
            sa.Column("response_data", JSON, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("pre_snapshot_id", UUID(as_uuid=True), sa.ForeignKey("router_config_snapshots.id"), nullable=True),
            sa.Column("post_snapshot_id", UUID(as_uuid=True), sa.ForeignKey("router_config_snapshots.id"), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_push_results_push_id", "router_config_push_results", ["push_id"])

    for val in [
        "router_config_push",
        "router_config_backup",
        "router_reboot",
        "router_firmware_upgrade",
        "router_bulk_push",
    ]:
        _add_enum_value_if_not_exists("networkoperationtype", val)


def downgrade() -> None:
    op.drop_table("router_config_push_results")
    op.drop_table("router_config_pushes")
    op.drop_table("router_config_templates")
    op.drop_table("router_config_snapshots")
    op.drop_table("router_interfaces")
    op.drop_table("routers")
    op.drop_table("jump_hosts")
```

- [ ] **Step 8: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/models/router_management.py app/models/__init__.py app/models/network_operation.py alembic/versions/005_router_management.py tests/test_router_management_models.py
git commit -m "feat(router-mgmt): add models and migration for router management module"
```

---

## Task 2: Pydantic Schemas

**Files:**
- Create: `app/schemas/router_management.py`
- Create: `tests/test_router_management_schemas.py`

- [ ] **Step 1: Write schema validation tests**

Create `tests/test_router_management_schemas.py`:

```python
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.router_management import (
    JumpHostCreate,
    JumpHostUpdate,
    RouterConfigPushCreate,
    RouterConfigTemplateCreate,
    RouterCreate,
    RouterUpdate,
)


def test_router_create_minimal():
    schema = RouterCreate(
        name="router-1",
        hostname="r1",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="secret",
    )
    assert schema.name == "router-1"
    assert schema.rest_api_port == 443
    assert schema.use_ssl is True
    assert schema.access_method == "direct"


def test_router_create_with_jump_host():
    jh_id = uuid.uuid4()
    schema = RouterCreate(
        name="router-2",
        hostname="r2",
        management_ip="10.0.0.2",
        rest_api_username="admin",
        rest_api_password="secret",
        access_method="jump_host",
        jump_host_id=jh_id,
    )
    assert schema.access_method == "jump_host"
    assert schema.jump_host_id == jh_id


def test_router_create_name_too_short():
    with pytest.raises(ValidationError):
        RouterCreate(
            name="",
            hostname="r1",
            management_ip="10.0.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        )


def test_router_update_partial():
    schema = RouterUpdate(name="new-name")
    data = schema.model_dump(exclude_unset=True)
    assert data == {"name": "new-name"}


def test_jump_host_create():
    schema = JumpHostCreate(
        name="jump-1",
        hostname="jump.example.com",
        username="tunnel",
        ssh_key="-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----",
    )
    assert schema.port == 22
    assert schema.ssh_key is not None


def test_jump_host_update_partial():
    schema = JumpHostUpdate(hostname="new-jump.example.com")
    data = schema.model_dump(exclude_unset=True)
    assert data == {"hostname": "new-jump.example.com"}


def test_config_template_create():
    schema = RouterConfigTemplateCreate(
        name="sfq-queues",
        template_body="/queue simple set [find] queue=sfq/sfq",
        category="queue",
        variables={"queue_type": {"type": "string", "default": "sfq"}},
    )
    assert schema.category == "queue"


def test_config_push_create():
    router_ids = [uuid.uuid4(), uuid.uuid4()]
    schema = RouterConfigPushCreate(
        commands=["/queue simple set [find] queue=sfq/sfq"],
        router_ids=router_ids,
    )
    assert len(schema.router_ids) == 2
    assert len(schema.commands) == 1


def test_config_push_create_empty_commands():
    with pytest.raises(ValidationError):
        RouterConfigPushCreate(
            commands=[],
            router_ids=[uuid.uuid4()],
        )


def test_config_push_create_empty_routers():
    with pytest.raises(ValidationError):
        RouterConfigPushCreate(
            commands=["/ip address print"],
            router_ids=[],
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create schemas file**

Create `app/schemas/router_management.py`:

```python
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class JumpHostCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    ssh_key: str | None = None
    ssh_password: str | None = None


class JumpHostUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_key: str | None = None
    ssh_password: str | None = None
    is_active: bool | None = None


class JumpHostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    hostname: str
    port: int
    username: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RouterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    hostname: str = Field(min_length=1, max_length=255)
    management_ip: str = Field(min_length=1, max_length=255)
    rest_api_port: int = Field(default=443, ge=1, le=65535)
    rest_api_username: str = Field(min_length=1, max_length=255)
    rest_api_password: str = Field(min_length=1, max_length=512)
    use_ssl: bool = True
    verify_tls: bool = False
    location: str | None = None
    notes: str | None = None
    tags: dict | None = None
    access_method: str = "direct"
    jump_host_id: uuid.UUID | None = None
    nas_device_id: uuid.UUID | None = None
    network_device_id: uuid.UUID | None = None


class RouterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    management_ip: str | None = Field(default=None, min_length=1, max_length=255)
    rest_api_port: int | None = Field(default=None, ge=1, le=65535)
    rest_api_username: str | None = Field(default=None, min_length=1, max_length=255)
    rest_api_password: str | None = Field(default=None, min_length=1, max_length=512)
    use_ssl: bool | None = None
    verify_tls: bool | None = None
    location: str | None = None
    notes: str | None = None
    tags: dict | None = None
    access_method: str | None = None
    jump_host_id: uuid.UUID | None = None
    nas_device_id: uuid.UUID | None = None
    network_device_id: uuid.UUID | None = None
    status: str | None = None


class RouterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    hostname: str
    management_ip: str
    rest_api_port: int
    use_ssl: bool
    verify_tls: bool
    routeros_version: str | None
    board_name: str | None
    architecture: str | None
    serial_number: str | None
    firmware_type: str | None
    location: str | None
    notes: str | None
    tags: dict | None
    access_method: str
    jump_host_id: uuid.UUID | None
    nas_device_id: uuid.UUID | None
    network_device_id: uuid.UUID | None
    status: str
    last_seen_at: datetime | None
    last_config_sync_at: datetime | None
    last_config_change_at: datetime | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RouterInterfaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    router_id: uuid.UUID
    name: str
    type: str
    mac_address: str | None
    is_running: bool
    is_disabled: bool
    rx_byte: int
    tx_byte: int
    rx_packet: int
    tx_packet: int
    last_link_up_time: str | None
    speed: str | None
    comment: str | None
    synced_at: datetime


class RouterConfigSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    router_id: uuid.UUID
    config_export: str
    config_hash: str
    source: str
    captured_by: uuid.UUID | None
    created_at: datetime


class RouterConfigTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    template_body: str = Field(min_length=1)
    category: str = "custom"
    variables: dict = Field(default_factory=dict)


class RouterConfigTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    template_body: str | None = Field(default=None, min_length=1)
    category: str | None = None
    variables: dict | None = None
    is_active: bool | None = None


class RouterConfigTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    template_body: str
    category: str
    variables: dict
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RouterConfigPushCreate(BaseModel):
    template_id: uuid.UUID | None = None
    commands: list[str] = Field(min_length=1)
    variable_values: dict | None = None
    router_ids: list[uuid.UUID] = Field(min_length=1)


class RouterConfigPushRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID | None
    commands: list
    variable_values: dict | None
    initiated_by: uuid.UUID
    status: str
    created_at: datetime
    completed_at: datetime | None


class RouterConfigPushResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    push_id: uuid.UUID
    router_id: uuid.UUID
    status: str
    response_data: dict | None
    error_message: str | None
    pre_snapshot_id: uuid.UUID | None
    post_snapshot_id: uuid.UUID | None
    duration_ms: int | None
    created_at: datetime


class RouterHealthRead(BaseModel):
    cpu_load: int
    free_memory: int
    total_memory: int
    uptime: str
    free_hdd_space: int
    total_hdd_space: int
    architecture_name: str
    board_name: str
    version: str


class ConnectionTestResult(BaseModel):
    success: bool
    message: str
    response_time_ms: int | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_schemas.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/schemas/router_management.py tests/test_router_management_schemas.py
git commit -m "feat(router-mgmt): add Pydantic schemas for router management"
```

---

## Task 3: Connection Service

**Files:**
- Create: `app/services/router_management/__init__.py`
- Create: `app/services/router_management/connection.py`
- Create: `tests/test_router_management_connection.py`

- [ ] **Step 1: Write connection service tests**

Create `tests/test_router_management_connection.py`:

```python
import pytest

from app.services.router_management.connection import (
    DANGEROUS_COMMANDS,
    RouterConnectionService,
    check_dangerous_commands,
)


def test_check_dangerous_commands_blocks_reset():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/system/reset-configuration"])


def test_check_dangerous_commands_blocks_shutdown():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/system/shutdown"])


def test_check_dangerous_commands_allows_safe():
    check_dangerous_commands([
        "/queue simple set [find] queue=sfq/sfq",
        "/ip address add address=10.0.0.1/24 interface=ether1",
    ])


def test_check_dangerous_commands_case_insensitive():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/System/Reset-Configuration"])


def test_build_base_url_ssl():
    url = RouterConnectionService._build_base_url(
        management_ip="10.0.0.1", port=443, use_ssl=True
    )
    assert url == "https://10.0.0.1:443"


def test_build_base_url_no_ssl():
    url = RouterConnectionService._build_base_url(
        management_ip="10.0.0.1", port=80, use_ssl=False
    )
    assert url == "http://10.0.0.1:80"


def test_dangerous_commands_list_is_not_empty():
    assert len(DANGEROUS_COMMANDS) >= 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_connection.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create the service package and connection service**

Create `app/services/router_management/__init__.py`:

```python
```

Create `app/services/router_management/connection.py`:

```python
import logging
import time

import httpx
from sshtunnel import SSHTunnelForwarder

from app.models.router_management import JumpHost, Router
from app.schemas.router_management import ConnectionTestResult
from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)

DANGEROUS_COMMANDS = [
    "/system/reset-configuration",
    "/system/shutdown",
    "/file/remove",
    "/user/remove",
]

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0


def check_dangerous_commands(commands: list[str]) -> None:
    for cmd in commands:
        cmd_lower = cmd.lower().strip()
        for dangerous in DANGEROUS_COMMANDS:
            if cmd_lower.startswith(dangerous):
                raise ValueError(
                    f"Dangerous command blocked: {cmd}. "
                    f"Commands matching {dangerous} are not allowed."
                )


class RouterConnectionService:
    _tunnels: dict[str, SSHTunnelForwarder] = {}

    @staticmethod
    def _build_base_url(management_ip: str, port: int, use_ssl: bool) -> str:
        scheme = "https" if use_ssl else "http"
        return f"{scheme}://{management_ip}:{port}"

    @classmethod
    def _get_or_create_tunnel(
        cls, router: Router, jump_host: JumpHost
    ) -> SSHTunnelForwarder:
        tunnel_key = f"{jump_host.id}:{router.management_ip}:{router.rest_api_port}"

        if tunnel_key in cls._tunnels:
            tunnel = cls._tunnels[tunnel_key]
            if tunnel.is_active:
                return tunnel
            del cls._tunnels[tunnel_key]

        ssh_key = decrypt_credential(jump_host.ssh_key)
        ssh_password = decrypt_credential(jump_host.ssh_password)

        kwargs: dict = {
            "ssh_username": jump_host.username,
            "remote_bind_address": (
                router.management_ip,
                router.rest_api_port,
            ),
        }
        if ssh_key:
            kwargs["ssh_pkey"] = ssh_key
        elif ssh_password:
            kwargs["ssh_password"] = ssh_password

        tunnel = SSHTunnelForwarder(
            (jump_host.hostname, jump_host.port),
            **kwargs,
        )
        tunnel.start()
        cls._tunnels[tunnel_key] = tunnel
        logger.info(
            "SSH tunnel opened: %s:%d -> localhost:%d via %s",
            router.management_ip,
            router.rest_api_port,
            tunnel.local_bind_port,
            jump_host.hostname,
        )
        return tunnel

    @classmethod
    def cleanup_idle_tunnels(cls) -> int:
        closed = 0
        dead_keys = []
        for key, tunnel in cls._tunnels.items():
            if not tunnel.is_active:
                dead_keys.append(key)
                closed += 1
                continue
        for key in dead_keys:
            try:
                cls._tunnels[key].stop()
            except Exception:
                pass
            del cls._tunnels[key]
        return closed

    @classmethod
    def close_all_tunnels(cls) -> None:
        for tunnel in cls._tunnels.values():
            try:
                tunnel.stop()
            except Exception:
                pass
        cls._tunnels.clear()

    @classmethod
    def get_client(cls, router: Router) -> httpx.Client:
        username = decrypt_credential(router.rest_api_username) or router.rest_api_username
        password = decrypt_credential(router.rest_api_password) or router.rest_api_password

        if router.access_method.value == "jump_host" and router.jump_host:
            tunnel = cls._get_or_create_tunnel(router, router.jump_host)
            base_url = cls._build_base_url(
                "127.0.0.1", tunnel.local_bind_port, router.use_ssl
            )
        else:
            base_url = cls._build_base_url(
                router.management_ip, router.rest_api_port, router.use_ssl
            )

        return httpx.Client(
            base_url=base_url,
            auth=(username, password),
            verify=router.verify_tls,
            timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
        )

    @classmethod
    def execute(
        cls,
        router: Router,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                with cls.get_client(router) as client:
                    response = client.request(
                        method=method,
                        url=f"/rest{path}",
                        json=payload,
                    )
                    response.raise_for_status()
                    return response.json() if response.text else {}
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Router %s attempt %d failed: %s. Retrying in %.1fs",
                        router.name,
                        attempt + 1,
                        str(exc),
                        wait,
                    )
                    time.sleep(wait)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Router {router.name} returned {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                ) from exc

        raise RuntimeError(
            f"Router {router.name} unreachable after {MAX_RETRIES} attempts: {last_error}"
        )

    @classmethod
    def execute_batch(
        cls, router: Router, commands: list[dict]
    ) -> list[dict]:
        results = []
        for cmd in commands:
            result = cls.execute(
                router,
                method=cmd.get("method", "POST"),
                path=cmd["path"],
                payload=cmd.get("payload"),
            )
            results.append(result)
        return results

    @classmethod
    def test_connection(cls, router: Router) -> ConnectionTestResult:
        start = time.time()
        try:
            data = cls.execute(router, "GET", "/system/resource")
            elapsed_ms = int((time.time() - start) * 1000)
            version = data.get("version", "unknown")
            return ConnectionTestResult(
                success=True,
                message=f"Connected. RouterOS {version}",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            return ConnectionTestResult(
                success=False,
                message=str(exc),
                response_time_ms=elapsed_ms,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_connection.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/services/router_management/__init__.py app/services/router_management/connection.py tests/test_router_management_connection.py
git commit -m "feat(router-mgmt): add RouterConnectionService with SSH tunnel support"
```

---

## Task 4: Inventory Service

**Files:**
- Create: `app/services/router_management/inventory.py`
- Create: `tests/test_router_management_inventory.py`

- [ ] **Step 1: Write inventory service tests**

Create `tests/test_router_management_inventory.py`:

```python
import uuid

import pytest
from fastapi import HTTPException

from app.models.router_management import (
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterInterface,
    RouterStatus,
)
from app.schemas.router_management import (
    JumpHostCreate,
    JumpHostUpdate,
    RouterCreate,
    RouterUpdate,
)
from app.services.router_management.inventory import (
    JumpHostInventory,
    RouterInventory,
)


def test_create_router(db_session):
    payload = RouterCreate(
        name="test-router-1",
        hostname="tr1",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="secret123",
    )
    router = RouterInventory.create(db_session, payload)
    assert router.name == "test-router-1"
    assert router.status == RouterStatus.offline
    assert router.is_active is True


def test_create_router_duplicate_name(db_session):
    payload = RouterCreate(
        name="dup-router",
        hostname="dr1",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="secret",
    )
    RouterInventory.create(db_session, payload)
    with pytest.raises(HTTPException, match="409"):
        RouterInventory.create(db_session, payload)


def test_get_router(db_session):
    payload = RouterCreate(
        name="get-router",
        hostname="gr1",
        management_ip="10.0.0.2",
        rest_api_username="admin",
        rest_api_password="secret",
    )
    created = RouterInventory.create(db_session, payload)
    fetched = RouterInventory.get(db_session, created.id)
    assert fetched.id == created.id
    assert fetched.name == "get-router"


def test_get_router_not_found(db_session):
    with pytest.raises(HTTPException, match="404"):
        RouterInventory.get(db_session, uuid.uuid4())


def test_list_routers(db_session):
    for i in range(3):
        RouterInventory.create(
            db_session,
            RouterCreate(
                name=f"list-router-{i}",
                hostname=f"lr{i}",
                management_ip=f"10.0.{i}.1",
                rest_api_username="admin",
                rest_api_password="secret",
            ),
        )
    routers = RouterInventory.list(db_session)
    assert len(routers) >= 3


def test_list_routers_filter_status(db_session):
    r = RouterInventory.create(
        db_session,
        RouterCreate(
            name="online-router",
            hostname="or1",
            management_ip="10.1.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )
    r.status = RouterStatus.online
    db_session.commit()

    online = RouterInventory.list(db_session, status="online")
    names = [x.name for x in online]
    assert "online-router" in names


def test_update_router(db_session):
    created = RouterInventory.create(
        db_session,
        RouterCreate(
            name="update-router",
            hostname="ur1",
            management_ip="10.2.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )
    updated = RouterInventory.update(
        db_session, created.id, RouterUpdate(location="Server Room A")
    )
    assert updated.location == "Server Room A"


def test_delete_router(db_session):
    created = RouterInventory.create(
        db_session,
        RouterCreate(
            name="delete-router",
            hostname="dr1",
            management_ip="10.3.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )
    RouterInventory.delete(db_session, created.id)
    with pytest.raises(HTTPException, match="404"):
        RouterInventory.get(db_session, created.id)


def test_create_jump_host(db_session):
    payload = JumpHostCreate(
        name="test-jh-1",
        hostname="jump.example.com",
        username="tunnel",
    )
    jh = JumpHostInventory.create(db_session, payload)
    assert jh.name == "test-jh-1"
    assert jh.port == 22


def test_list_jump_hosts(db_session):
    JumpHostInventory.create(
        db_session,
        JumpHostCreate(name="jh-list-1", hostname="j1.example.com", username="t"),
    )
    hosts = JumpHostInventory.list(db_session)
    assert len(hosts) >= 1


def test_update_jump_host(db_session):
    jh = JumpHostInventory.create(
        db_session,
        JumpHostCreate(name="jh-update", hostname="j2.example.com", username="t"),
    )
    updated = JumpHostInventory.update(
        db_session, jh.id, JumpHostUpdate(port=2222)
    )
    assert updated.port == 2222


def test_delete_jump_host(db_session):
    jh = JumpHostInventory.create(
        db_session,
        JumpHostCreate(name="jh-delete", hostname="j3.example.com", username="t"),
    )
    JumpHostInventory.delete(db_session, jh.id)
    with pytest.raises(HTTPException, match="404"):
        JumpHostInventory.get(db_session, jh.id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_inventory.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create inventory service**

Create `app/services/router_management/inventory.py`:

```python
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.router_management import (
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterInterface,
    RouterStatus,
)
from app.schemas.router_management import (
    JumpHostCreate,
    JumpHostUpdate,
    RouterCreate,
    RouterUpdate,
)
from app.services.common import apply_ordering, apply_pagination
from app.services.credential_crypto import encrypt_credential
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

ROUTER_CREDENTIAL_FIELDS = ("rest_api_password",)
JUMP_HOST_CREDENTIAL_FIELDS = ("ssh_key", "ssh_password")


class RouterInventory(ListResponseMixin):
    ALLOWED_ORDER_COLUMNS = {
        "name": Router.name,
        "hostname": Router.hostname,
        "management_ip": Router.management_ip,
        "status": Router.status,
        "created_at": Router.created_at,
    }

    @staticmethod
    def create(db: Session, payload: RouterCreate) -> Router:
        data = payload.model_dump(exclude_unset=True)

        for field in ROUTER_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        if data.get("access_method"):
            data["access_method"] = RouterAccessMethod(data["access_method"])

        if data.get("jump_host_id"):
            jh = db.get(JumpHost, data["jump_host_id"])
            if not jh:
                raise HTTPException(status_code=404, detail="Jump host not found")

        router = Router(**data)
        try:
            db.add(router)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409, detail=f"Router with name '{payload.name}' already exists"
            )
        db.refresh(router)
        logger.info("Router created: %s (%s)", router.name, router.id)
        return router

    @staticmethod
    def get(db: Session, router_id: uuid.UUID) -> Router:
        router = db.execute(
            select(Router).where(Router.id == router_id, Router.is_active.is_(True))
        ).scalar_one_or_none()
        if not router:
            raise HTTPException(status_code=404, detail="Router not found")
        return router

    @staticmethod
    def list(
        db: Session,
        status: str | None = None,
        access_method: str | None = None,
        jump_host_id: uuid.UUID | None = None,
        search: str | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Router]:
        query = select(Router).where(Router.is_active.is_(True))

        if status:
            query = query.where(Router.status == RouterStatus(status))
        if access_method:
            query = query.where(
                Router.access_method == RouterAccessMethod(access_method)
            )
        if jump_host_id:
            query = query.where(Router.jump_host_id == jump_host_id)
        if search:
            pattern = f"%{search}%"
            query = query.where(
                Router.name.ilike(pattern)
                | Router.hostname.ilike(pattern)
                | Router.management_ip.ilike(pattern)
                | Router.location.ilike(pattern)
            )

        query = apply_ordering(
            query, order_by, order_dir, RouterInventory.ALLOWED_ORDER_COLUMNS
        )
        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(db: Session, status: str | None = None) -> int:
        query = select(func.count(Router.id)).where(Router.is_active.is_(True))
        if status:
            query = query.where(Router.status == RouterStatus(status))
        return db.execute(query).scalar_one()

    @staticmethod
    def update(db: Session, router_id: uuid.UUID, payload: RouterUpdate) -> Router:
        router = RouterInventory.get(db, router_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ROUTER_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        if "access_method" in data and data["access_method"]:
            data["access_method"] = RouterAccessMethod(data["access_method"])
        if "status" in data and data["status"]:
            data["status"] = RouterStatus(data["status"])

        for key, value in data.items():
            setattr(router, key, value)

        db.commit()
        db.refresh(router)
        logger.info("Router updated: %s (%s)", router.name, router.id)
        return router

    @staticmethod
    def delete(db: Session, router_id: uuid.UUID) -> None:
        router = RouterInventory.get(db, router_id)
        router.is_active = False
        db.commit()
        logger.info("Router soft-deleted: %s (%s)", router.name, router.id)

    @staticmethod
    def upsert_interfaces(
        db: Session, router: Router, interfaces_data: list[dict]
    ) -> list[RouterInterface]:
        now = datetime.now(timezone.utc)
        existing = {
            iface.name: iface
            for iface in db.execute(
                select(RouterInterface).where(RouterInterface.router_id == router.id)
            ).scalars().all()
        }

        seen_names: set[str] = set()
        results: list[RouterInterface] = []

        for data in interfaces_data:
            name = data.get("name", "")
            seen_names.add(name)

            if name in existing:
                iface = existing[name]
                for key, value in data.items():
                    if key != "name":
                        setattr(iface, key, value)
                iface.synced_at = now
            else:
                iface = RouterInterface(router_id=router.id, synced_at=now, **data)
                db.add(iface)
            results.append(iface)

        for name, iface in existing.items():
            if name not in seen_names:
                db.delete(iface)

        db.commit()
        return results


class JumpHostInventory:
    @staticmethod
    def create(db: Session, payload: JumpHostCreate) -> JumpHost:
        data = payload.model_dump(exclude_unset=True)
        for field in JUMP_HOST_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        jh = JumpHost(**data)
        try:
            db.add(jh)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Jump host with name '{payload.name}' already exists",
            )
        db.refresh(jh)
        logger.info("Jump host created: %s (%s)", jh.name, jh.id)
        return jh

    @staticmethod
    def get(db: Session, jh_id: uuid.UUID) -> JumpHost:
        jh = db.execute(
            select(JumpHost).where(JumpHost.id == jh_id, JumpHost.is_active.is_(True))
        ).scalar_one_or_none()
        if not jh:
            raise HTTPException(status_code=404, detail="Jump host not found")
        return jh

    @staticmethod
    def list(db: Session, limit: int = 50, offset: int = 0) -> list[JumpHost]:
        query = (
            select(JumpHost)
            .where(JumpHost.is_active.is_(True))
            .order_by(JumpHost.name)
            .limit(limit)
            .offset(offset)
        )
        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(db: Session, jh_id: uuid.UUID, payload: JumpHostUpdate) -> JumpHost:
        jh = JumpHostInventory.get(db, jh_id)
        data = payload.model_dump(exclude_unset=True)
        for field in JUMP_HOST_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        for key, value in data.items():
            setattr(jh, key, value)

        db.commit()
        db.refresh(jh)
        logger.info("Jump host updated: %s (%s)", jh.name, jh.id)
        return jh

    @staticmethod
    def delete(db: Session, jh_id: uuid.UUID) -> None:
        jh = JumpHostInventory.get(db, jh_id)
        jh.is_active = False
        db.commit()
        logger.info("Jump host soft-deleted: %s (%s)", jh.name, jh.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_inventory.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/services/router_management/inventory.py tests/test_router_management_inventory.py
git commit -m "feat(router-mgmt): add RouterInventory and JumpHostInventory services"
```

---

## Task 5: Config Service

**Files:**
- Create: `app/services/router_management/config.py`
- Create: `tests/test_router_management_config.py`

- [ ] **Step 1: Write config service tests**

Create `tests/test_router_management_config.py`:

```python
import uuid

import pytest
from fastapi import HTTPException

from app.models.router_management import (
    Router,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterTemplateCategory,
)
from app.schemas.router_management import (
    RouterConfigPushCreate,
    RouterConfigTemplateCreate,
    RouterConfigTemplateUpdate,
)
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)


def _make_router(db_session, name: str) -> Router:
    r = Router(
        name=name,
        hostname=name,
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def test_store_snapshot(db_session):
    router = _make_router(db_session, "snap-store-test")
    snap = RouterConfigService.store_snapshot(
        db_session,
        router_id=router.id,
        config_export="/ip address\nadd address=10.0.0.1/24 interface=ether1",
        source=RouterSnapshotSource.manual,
    )
    assert snap.router_id == router.id
    assert snap.config_hash is not None
    assert len(snap.config_hash) == 64


def test_list_snapshots(db_session):
    router = _make_router(db_session, "snap-list-test")
    for i in range(3):
        RouterConfigService.store_snapshot(
            db_session,
            router_id=router.id,
            config_export=f"config version {i}",
            source=RouterSnapshotSource.scheduled,
        )
    snaps = RouterConfigService.list_snapshots(db_session, router.id)
    assert len(snaps) == 3


def test_get_snapshot(db_session):
    router = _make_router(db_session, "snap-get-test")
    snap = RouterConfigService.store_snapshot(
        db_session,
        router_id=router.id,
        config_export="test config",
        source=RouterSnapshotSource.manual,
    )
    fetched = RouterConfigService.get_snapshot(db_session, snap.id)
    assert fetched.config_export == "test config"


def test_create_template(db_session):
    tmpl = RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(
            name="test-template",
            template_body="/queue simple set [find] queue={{ queue_type }}/{{ queue_type }}",
            category="queue",
            variables={"queue_type": {"type": "string", "default": "sfq"}},
        ),
    )
    assert tmpl.name == "test-template"
    assert tmpl.category == RouterTemplateCategory.queue


def test_update_template(db_session):
    tmpl = RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(
            name="update-tmpl",
            template_body="original body",
        ),
    )
    updated = RouterTemplateService.update(
        db_session, tmpl.id, RouterConfigTemplateUpdate(template_body="new body")
    )
    assert updated.template_body == "new body"


def test_list_templates(db_session):
    RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(name="list-tmpl-1", template_body="body1"),
    )
    templates = RouterTemplateService.list(db_session)
    assert len(templates) >= 1


def test_render_template():
    body = "/ip dns set servers={{ dns_servers }}"
    variables = {"dns_servers": "8.8.8.8,8.8.4.4"}
    result = RouterConfigService.render_template(body, variables)
    assert result == "/ip dns set servers=8.8.8.8,8.8.4.4"


def test_render_template_missing_var():
    body = "/ip dns set servers={{ dns_servers }}"
    with pytest.raises(ValueError, match="Template rendering failed"):
        RouterConfigService.render_template(body, {})


def test_create_push_record(db_session):
    router = _make_router(db_session, "push-test")
    user_id = uuid.uuid4()

    push = RouterConfigService.create_push(
        db_session,
        commands=["/queue simple set [find] queue=sfq/sfq"],
        router_ids=[router.id],
        initiated_by=user_id,
    )
    assert push.status == RouterConfigPushStatus.pending
    assert len(push.results) == 1
    assert push.results[0].router_id == router.id


def test_create_push_dangerous_command(db_session):
    router = _make_router(db_session, "push-danger-test")
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        RouterConfigService.create_push(
            db_session,
            commands=["/system/reset-configuration"],
            router_ids=[router.id],
            initiated_by=uuid.uuid4(),
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_config.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create config service**

Create `app/services/router_management/config.py`:

```python
import hashlib
import logging
import uuid

from fastapi import HTTPException
from jinja2 import BaseLoader, Environment, TemplateSyntaxError, UndefinedError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.router_management import (
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterTemplateCategory,
)
from app.schemas.router_management import (
    RouterConfigTemplateCreate,
    RouterConfigTemplateUpdate,
)
from app.services.router_management.connection import check_dangerous_commands

logger = logging.getLogger(__name__)

_jinja_env = Environment(loader=BaseLoader(), undefined="strict")


class RouterConfigService:
    @staticmethod
    def store_snapshot(
        db: Session,
        router_id: uuid.UUID,
        config_export: str,
        source: RouterSnapshotSource,
        captured_by: uuid.UUID | None = None,
    ) -> RouterConfigSnapshot:
        config_hash = hashlib.sha256(config_export.encode()).hexdigest()

        snap = RouterConfigSnapshot(
            router_id=router_id,
            config_export=config_export,
            config_hash=config_hash,
            source=source,
            captured_by=captured_by,
        )
        db.add(snap)
        db.commit()
        db.refresh(snap)
        logger.info("Config snapshot stored for router %s: %s", router_id, snap.id)
        return snap

    @staticmethod
    def list_snapshots(
        db: Session,
        router_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RouterConfigSnapshot]:
        query = (
            select(RouterConfigSnapshot)
            .where(RouterConfigSnapshot.router_id == router_id)
            .order_by(RouterConfigSnapshot.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(db.execute(query).scalars().all())

    @staticmethod
    def get_snapshot(db: Session, snapshot_id: uuid.UUID) -> RouterConfigSnapshot:
        snap = db.get(RouterConfigSnapshot, snapshot_id)
        if not snap:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return snap

    @staticmethod
    def render_template(template_body: str, variables: dict) -> str:
        try:
            template = _jinja_env.from_string(template_body)
            return template.render(**variables)
        except (TemplateSyntaxError, UndefinedError, TypeError) as exc:
            raise ValueError(f"Template rendering failed: {exc}") from exc

    @staticmethod
    def create_push(
        db: Session,
        commands: list[str],
        router_ids: list[uuid.UUID],
        initiated_by: uuid.UUID,
        template_id: uuid.UUID | None = None,
        variable_values: dict | None = None,
    ) -> RouterConfigPush:
        check_dangerous_commands(commands)

        push = RouterConfigPush(
            template_id=template_id,
            commands=commands,
            variable_values=variable_values,
            initiated_by=initiated_by,
            status=RouterConfigPushStatus.pending,
        )
        db.add(push)
        db.flush()

        for rid in router_ids:
            result = RouterConfigPushResult(
                push_id=push.id,
                router_id=rid,
                status=RouterPushResultStatus.pending,
            )
            db.add(result)

        db.commit()
        db.refresh(push)
        logger.info(
            "Config push created: %s (%d routers, %d commands)",
            push.id,
            len(router_ids),
            len(commands),
        )
        return push

    @staticmethod
    def get_push(db: Session, push_id: uuid.UUID) -> RouterConfigPush:
        push = db.get(RouterConfigPush, push_id)
        if not push:
            raise HTTPException(status_code=404, detail="Config push not found")
        return push

    @staticmethod
    def list_pushes(
        db: Session, limit: int = 50, offset: int = 0
    ) -> list[RouterConfigPush]:
        query = (
            select(RouterConfigPush)
            .order_by(RouterConfigPush.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(db.execute(query).scalars().all())


class RouterTemplateService:
    @staticmethod
    def create(
        db: Session, payload: RouterConfigTemplateCreate
    ) -> RouterConfigTemplate:
        data = payload.model_dump(exclude_unset=True)
        if "category" in data:
            data["category"] = RouterTemplateCategory(data["category"])

        tmpl = RouterConfigTemplate(**data)
        try:
            db.add(tmpl)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Template with name '{payload.name}' already exists",
            )
        db.refresh(tmpl)
        logger.info("Config template created: %s (%s)", tmpl.name, tmpl.id)
        return tmpl

    @staticmethod
    def get(db: Session, template_id: uuid.UUID) -> RouterConfigTemplate:
        tmpl = db.get(RouterConfigTemplate, template_id)
        if not tmpl:
            raise HTTPException(status_code=404, detail="Template not found")
        return tmpl

    @staticmethod
    def list(
        db: Session,
        category: str | None = None,
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RouterConfigTemplate]:
        query = select(RouterConfigTemplate)
        if active_only:
            query = query.where(RouterConfigTemplate.is_active.is_(True))
        if category:
            query = query.where(
                RouterConfigTemplate.category == RouterTemplateCategory(category)
            )
        query = query.order_by(RouterConfigTemplate.name).limit(limit).offset(offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(
        db: Session,
        template_id: uuid.UUID,
        payload: RouterConfigTemplateUpdate,
    ) -> RouterConfigTemplate:
        tmpl = RouterTemplateService.get(db, template_id)
        data = payload.model_dump(exclude_unset=True)
        if "category" in data and data["category"]:
            data["category"] = RouterTemplateCategory(data["category"])

        for key, value in data.items():
            setattr(tmpl, key, value)

        db.commit()
        db.refresh(tmpl)
        logger.info("Config template updated: %s (%s)", tmpl.name, tmpl.id)
        return tmpl

    @staticmethod
    def delete(db: Session, template_id: uuid.UUID) -> None:
        tmpl = RouterTemplateService.get(db, template_id)
        tmpl.is_active = False
        db.commit()
        logger.info("Config template soft-deleted: %s (%s)", tmpl.name, tmpl.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_config.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/services/router_management/config.py tests/test_router_management_config.py
git commit -m "feat(router-mgmt): add RouterConfigService and RouterTemplateService"
```

---

## Task 6: Monitoring Service

**Files:**
- Create: `app/services/router_management/monitoring.py`
- Create: `tests/test_router_management_monitoring.py`

- [ ] **Step 1: Write monitoring service tests**

Create `tests/test_router_management_monitoring.py`:

```python
from app.models.router_management import Router, RouterStatus
from app.services.router_management.monitoring import RouterMonitoringService


def _make_routers(db_session, count: int) -> list[Router]:
    routers = []
    for i in range(count):
        r = Router(
            name=f"mon-router-{i}",
            hostname=f"mr{i}",
            management_ip=f"10.0.{i}.1",
            rest_api_username="admin",
            rest_api_password="enc:test",
            status=RouterStatus.online if i % 2 == 0 else RouterStatus.offline,
        )
        db_session.add(r)
        routers.append(r)
    db_session.commit()
    for r in routers:
        db_session.refresh(r)
    return routers


def test_dashboard_summary(db_session):
    _make_routers(db_session, 4)
    summary = RouterMonitoringService.get_dashboard_summary(db_session)
    assert summary["total"] >= 4
    assert "online" in summary
    assert "offline" in summary
    assert "degraded" in summary
    assert "maintenance" in summary
    assert "unreachable" in summary


def test_dashboard_summary_empty(db_session):
    summary = RouterMonitoringService.get_dashboard_summary(db_session)
    assert summary["total"] >= 0
    assert summary["online"] >= 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_monitoring.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create monitoring service**

Create `app/services/router_management/monitoring.py`:

```python
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.router_management import Router, RouterStatus

logger = logging.getLogger(__name__)


class RouterMonitoringService:
    @staticmethod
    def get_dashboard_summary(db: Session) -> dict:
        counts = {}
        for status in RouterStatus:
            count = db.execute(
                select(func.count(Router.id)).where(
                    Router.is_active.is_(True),
                    Router.status == status,
                )
            ).scalar_one()
            counts[status.value] = count

        total = sum(counts.values())
        return {"total": total, **counts}

    @staticmethod
    def parse_health_response(data: dict) -> dict:
        return {
            "cpu_load": int(data.get("cpu-load", 0)),
            "free_memory": int(data.get("free-memory", 0)),
            "total_memory": int(data.get("total-memory", 0)),
            "uptime": data.get("uptime", "unknown"),
            "free_hdd_space": int(data.get("free-hdd-space", 0)),
            "total_hdd_space": int(data.get("total-hdd-space", 0)),
            "architecture_name": data.get("architecture-name", "unknown"),
            "board_name": data.get("board-name", "unknown"),
            "version": data.get("version", "unknown"),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_monitoring.py -v`
Expected: All 2 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/services/router_management/monitoring.py tests/test_router_management_monitoring.py
git commit -m "feat(router-mgmt): add RouterMonitoringService with dashboard summary"
```

---

## Task 7: Settings and App Registration

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`

- [ ] **Step 1: Add router management settings to config.py**

Add to the `Settings` dataclass in `app/config.py`:

```python
    # Router Management
    router_sync_interval_hours: int = int(os.getenv("ROUTER_SYNC_INTERVAL_HOURS", "6"))
    router_interface_sync_interval_min: int = int(os.getenv("ROUTER_IFACE_SYNC_INTERVAL_MIN", "15"))
    router_snapshot_schedule: str = os.getenv("ROUTER_SNAPSHOT_SCHEDULE", "0 2 * * *")
    router_tunnel_cleanup_interval_min: int = int(os.getenv("ROUTER_TUNNEL_CLEANUP_MIN", "5"))
```

- [ ] **Step 2: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/config.py
git commit -m "feat(router-mgmt): add router management config settings"
```

Note: `app/main.py` router registration will be done in Task 8 after the API routes are created.

---

## Task 8: API Endpoints

**Files:**
- Create: `app/api/router_management.py`
- Modify: `app/main.py`
- Create: `tests/test_router_management_api.py`

- [ ] **Step 1: Write API tests**

Create `tests/test_router_management_api.py`:

```python
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.router_management import Router


@pytest.fixture()
def client(db_session):
    return TestClient(app)


def test_list_routers(client, db_session):
    r = Router(
        name="api-list-test",
        hostname="alt",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(r)
    db_session.commit()

    response = client.get("/api/v1/network/routers")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data


def test_create_router(client):
    response = client.post(
        "/api/v1/network/routers",
        json={
            "name": "api-create-test",
            "hostname": "act",
            "management_ip": "10.0.0.2",
            "rest_api_username": "admin",
            "rest_api_password": "secret123",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "api-create-test"


def test_get_router(client, db_session):
    r = Router(
        name="api-get-test",
        hostname="agt",
        management_ip="10.0.0.3",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)

    response = client.get(f"/api/v1/network/routers/{r.id}")
    assert response.status_code == 200
    assert response.json()["name"] == "api-get-test"


def test_get_router_not_found(client):
    response = client.get(f"/api/v1/network/routers/{uuid.uuid4()}")
    assert response.status_code == 404


def test_update_router(client, db_session):
    r = Router(
        name="api-update-test",
        hostname="aut",
        management_ip="10.0.0.4",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)

    response = client.patch(
        f"/api/v1/network/routers/{r.id}",
        json={"location": "DC1 Rack 5"},
    )
    assert response.status_code == 200
    assert response.json()["location"] == "DC1 Rack 5"


def test_delete_router(client, db_session):
    r = Router(
        name="api-delete-test",
        hostname="adt",
        management_ip="10.0.0.5",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)

    response = client.delete(f"/api/v1/network/routers/{r.id}")
    assert response.status_code == 200

    response = client.get(f"/api/v1/network/routers/{r.id}")
    assert response.status_code == 404


def test_create_config_template(client):
    response = client.post(
        "/api/v1/network/routers/config-templates",
        json={
            "name": "api-tmpl-test",
            "template_body": "/queue simple set [find] queue=sfq/sfq",
            "category": "queue",
        },
    )
    assert response.status_code == 200
    assert response.json()["name"] == "api-tmpl-test"


def test_list_config_templates(client, db_session):
    response = client.get("/api/v1/network/routers/config-templates")
    assert response.status_code == 200
    assert "items" in response.json()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_api.py -v`
Expected: FAIL — `ModuleNotFoundError` or 404 (routes not registered)

- [ ] **Step 3: Create API routes**

Create `app/api/router_management.py`:

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.router_management import (
    ConnectionTestResult,
    JumpHostCreate,
    JumpHostRead,
    JumpHostUpdate,
    RouterConfigPushCreate,
    RouterConfigPushRead,
    RouterConfigPushResultRead,
    RouterConfigSnapshotRead,
    RouterConfigTemplateCreate,
    RouterConfigTemplateRead,
    RouterConfigTemplateUpdate,
    RouterCreate,
    RouterHealthRead,
    RouterInterfaceRead,
    RouterRead,
    RouterUpdate,
)
from app.services.auth_dependencies import require_permission
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)
from app.services.router_management.connection import RouterConnectionService
from app.services.router_management.inventory import (
    JumpHostInventory,
    RouterInventory,
)
from app.services.router_management.monitoring import RouterMonitoringService

router = APIRouter(prefix="/network/routers", tags=["router-management"])


# --- Router CRUD ---


@router.get(
    "",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_routers(
    status: str | None = None,
    access_method: str | None = None,
    jump_host_id: uuid.UUID | None = None,
    search: str | None = None,
    order_by: str = "name",
    order_dir: str = "asc",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    items = RouterInventory.list(
        db,
        status=status,
        access_method=access_method,
        jump_host_id=jump_host_id,
        search=search,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    count = RouterInventory.count(db, status=status)
    return {"items": [RouterRead.model_validate(r) for r in items], "count": count, "limit": limit, "offset": offset}


@router.post(
    "",
    response_model=RouterRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def create_router(
    payload: RouterCreate,
    db: Session = Depends(get_db),
) -> RouterRead:
    r = RouterInventory.create(db, payload)
    return RouterRead.model_validate(r)


@router.get(
    "/{router_id}",
    response_model=RouterRead,
    dependencies=[Depends(require_permission("router:read"))],
)
def get_router(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterRead:
    r = RouterInventory.get(db, router_id)
    return RouterRead.model_validate(r)


@router.patch(
    "/{router_id}",
    response_model=RouterRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def update_router(
    router_id: uuid.UUID,
    payload: RouterUpdate,
    db: Session = Depends(get_db),
) -> RouterRead:
    r = RouterInventory.update(db, router_id, payload)
    return RouterRead.model_validate(r)


@router.delete(
    "/{router_id}",
    dependencies=[Depends(require_permission("router:write"))],
)
def delete_router(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    RouterInventory.delete(db, router_id)
    return {"detail": "Router deleted"}


# --- Router Actions ---


@router.post(
    "/{router_id}/test-connection",
    response_model=ConnectionTestResult,
    dependencies=[Depends(require_permission("router:read"))],
)
def test_router_connection(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> ConnectionTestResult:
    r = RouterInventory.get(db, router_id)
    return RouterConnectionService.test_connection(r)


@router.post(
    "/{router_id}/sync",
    dependencies=[Depends(require_permission("router:write"))],
)
def sync_router(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    r = RouterInventory.get(db, router_id)
    try:
        sys_data = RouterConnectionService.execute(r, "GET", "/system/resource")
        rb_data = RouterConnectionService.execute(r, "GET", "/system/routerboard")

        from app.schemas.router_management import RouterUpdate as RU
        from datetime import datetime, timezone

        RouterInventory.update(
            db,
            r.id,
            RU(
                status="online",
            ),
        )
        r.routeros_version = sys_data.get("version")
        r.board_name = sys_data.get("board-name") or rb_data.get("model")
        r.architecture = sys_data.get("architecture-name")
        r.serial_number = rb_data.get("serial-number")
        r.firmware_type = rb_data.get("firmware-type")
        r.last_seen_at = datetime.now(timezone.utc)
        db.commit()

        iface_data = RouterConnectionService.execute(r, "GET", "/interface")
        if isinstance(iface_data, list):
            interfaces = [
                {
                    "name": i.get("name", ""),
                    "type": i.get("type", "ether"),
                    "mac_address": i.get("mac-address"),
                    "is_running": i.get("running", "false") == "true",
                    "is_disabled": i.get("disabled", "false") == "true",
                    "rx_byte": int(i.get("rx-byte", 0)),
                    "tx_byte": int(i.get("tx-byte", 0)),
                    "rx_packet": int(i.get("rx-packet", 0)),
                    "tx_packet": int(i.get("tx-packet", 0)),
                    "last_link_up_time": i.get("last-link-up-time"),
                    "speed": i.get("actual-mtu"),
                    "comment": i.get("comment"),
                }
                for i in iface_data
            ]
            RouterInventory.upsert_interfaces(db, r, interfaces)

        return {"detail": "Sync complete", "version": r.routeros_version}
    except Exception as exc:
        RouterInventory.update(db, r.id, RU(status="unreachable"))
        raise HTTPException(status_code=502, detail=str(exc))


@router.get(
    "/{router_id}/health",
    response_model=RouterHealthRead,
    dependencies=[Depends(require_permission("router:read"))],
)
def get_router_health(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterHealthRead:
    r = RouterInventory.get(db, router_id)
    data = RouterConnectionService.execute(r, "GET", "/system/resource")
    parsed = RouterMonitoringService.parse_health_response(data)
    return RouterHealthRead(**parsed)


@router.get(
    "/{router_id}/interfaces",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_router_interfaces(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    from sqlalchemy import select
    from app.models.router_management import RouterInterface

    RouterInventory.get(db, router_id)
    query = (
        select(RouterInterface)
        .where(RouterInterface.router_id == router_id)
        .order_by(RouterInterface.name)
    )
    interfaces = list(db.execute(query).scalars().all())
    return {"items": [RouterInterfaceRead.model_validate(i) for i in interfaces]}


# --- Config Snapshots ---


@router.get(
    "/{router_id}/snapshots",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_snapshots(
    router_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    RouterInventory.get(db, router_id)
    snaps = RouterConfigService.list_snapshots(db, router_id, limit=limit, offset=offset)
    return {"items": [RouterConfigSnapshotRead.model_validate(s) for s in snaps]}


@router.post(
    "/{router_id}/snapshots",
    response_model=RouterConfigSnapshotRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def capture_snapshot(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterConfigSnapshotRead:
    from app.models.router_management import RouterSnapshotSource

    r = RouterInventory.get(db, router_id)
    try:
        data = RouterConnectionService.execute(r, "GET", "/export")
        config_text = data if isinstance(data, str) else str(data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to export config: {exc}")

    snap = RouterConfigService.store_snapshot(
        db, router_id=r.id, config_export=config_text, source=RouterSnapshotSource.manual
    )
    return RouterConfigSnapshotRead.model_validate(snap)


@router.get(
    "/{router_id}/snapshots/{snapshot_id}",
    response_model=RouterConfigSnapshotRead,
    dependencies=[Depends(require_permission("router:read"))],
)
def get_snapshot(
    router_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterConfigSnapshotRead:
    RouterInventory.get(db, router_id)
    snap = RouterConfigService.get_snapshot(db, snapshot_id)
    return RouterConfigSnapshotRead.model_validate(snap)


# --- Config Templates ---


@router.get(
    "/config-templates",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_templates(
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    templates = RouterTemplateService.list(db, category=category, limit=limit, offset=offset)
    return {"items": [RouterConfigTemplateRead.model_validate(t) for t in templates]}


@router.post(
    "/config-templates",
    response_model=RouterConfigTemplateRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def create_template(
    payload: RouterConfigTemplateCreate,
    db: Session = Depends(get_db),
) -> RouterConfigTemplateRead:
    tmpl = RouterTemplateService.create(db, payload)
    return RouterConfigTemplateRead.model_validate(tmpl)


@router.patch(
    "/config-templates/{template_id}",
    response_model=RouterConfigTemplateRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def update_template(
    template_id: uuid.UUID,
    payload: RouterConfigTemplateUpdate,
    db: Session = Depends(get_db),
) -> RouterConfigTemplateRead:
    tmpl = RouterTemplateService.update(db, template_id, payload)
    return RouterConfigTemplateRead.model_validate(tmpl)


@router.post(
    "/config-templates/{template_id}/preview",
    dependencies=[Depends(require_permission("router:read"))],
)
def preview_template(
    template_id: uuid.UUID,
    variables: dict,
    db: Session = Depends(get_db),
) -> dict:
    tmpl = RouterTemplateService.get(db, template_id)
    try:
        rendered = RouterConfigService.render_template(tmpl.template_body, variables)
        return {"rendered": rendered}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# --- Config Pushes ---


@router.post(
    "/config-pushes",
    response_model=RouterConfigPushRead,
    dependencies=[Depends(require_permission("router:push_config"))],
)
def create_push(
    payload: RouterConfigPushCreate,
    db: Session = Depends(get_db),
) -> RouterConfigPushRead:
    from app.services.auth_dependencies import get_current_user_id

    try:
        user_id = get_current_user_id()
    except Exception:
        user_id = uuid.uuid4()

    try:
        push = RouterConfigService.create_push(
            db,
            commands=payload.commands,
            router_ids=payload.router_ids,
            initiated_by=user_id,
            template_id=payload.template_id,
            variable_values=payload.variable_values,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    from app.tasks.router_sync import execute_config_push

    execute_config_push.delay(str(push.id))

    return RouterConfigPushRead.model_validate(push)


@router.get(
    "/config-pushes",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_pushes(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    pushes = RouterConfigService.list_pushes(db, limit=limit, offset=offset)
    return {"items": [RouterConfigPushRead.model_validate(p) for p in pushes]}


@router.get(
    "/config-pushes/{push_id}",
    dependencies=[Depends(require_permission("router:read"))],
)
def get_push(
    push_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    push = RouterConfigService.get_push(db, push_id)
    results = [RouterConfigPushResultRead.model_validate(r) for r in push.results]
    push_data = RouterConfigPushRead.model_validate(push)
    return {"push": push_data, "results": results}


# --- Jump Hosts ---

jump_host_router = APIRouter(prefix="/network/jump-hosts", tags=["router-management"])


@jump_host_router.get(
    "",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_jump_hosts(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    hosts = JumpHostInventory.list(db, limit=limit, offset=offset)
    return {"items": [JumpHostRead.model_validate(h) for h in hosts]}


@jump_host_router.post(
    "",
    response_model=JumpHostRead,
    dependencies=[Depends(require_permission("router:admin"))],
)
def create_jump_host(
    payload: JumpHostCreate,
    db: Session = Depends(get_db),
) -> JumpHostRead:
    jh = JumpHostInventory.create(db, payload)
    return JumpHostRead.model_validate(jh)


@jump_host_router.patch(
    "/{jh_id}",
    response_model=JumpHostRead,
    dependencies=[Depends(require_permission("router:admin"))],
)
def update_jump_host(
    jh_id: uuid.UUID,
    payload: JumpHostUpdate,
    db: Session = Depends(get_db),
) -> JumpHostRead:
    jh = JumpHostInventory.update(db, jh_id, payload)
    return JumpHostRead.model_validate(jh)


@jump_host_router.delete(
    "/{jh_id}",
    dependencies=[Depends(require_permission("router:admin"))],
)
def delete_jump_host(
    jh_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    JumpHostInventory.delete(db, jh_id)
    return {"detail": "Jump host deleted"}


@jump_host_router.post(
    "/{jh_id}/test",
    dependencies=[Depends(require_permission("router:admin"))],
)
def test_jump_host(
    jh_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    from sshtunnel import SSHTunnelForwarder
    from app.services.credential_crypto import decrypt_credential

    jh = JumpHostInventory.get(db, jh_id)
    try:
        kwargs: dict = {"ssh_username": jh.username}
        ssh_key = decrypt_credential(jh.ssh_key)
        ssh_password = decrypt_credential(jh.ssh_password)
        if ssh_key:
            kwargs["ssh_pkey"] = ssh_key
        elif ssh_password:
            kwargs["ssh_password"] = ssh_password

        tunnel = SSHTunnelForwarder(
            (jh.hostname, jh.port),
            remote_bind_address=("127.0.0.1", 22),
            **kwargs,
        )
        tunnel.start()
        tunnel.stop()
        return {"success": True, "message": "SSH connection successful"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


# --- Dashboard ---


@router.get(
    "/dashboard",
    dependencies=[Depends(require_permission("router:read"))],
)
def router_dashboard(db: Session = Depends(get_db)) -> dict:
    return RouterMonitoringService.get_dashboard_summary(db)
```

- [ ] **Step 4: Register routes in app/main.py**

Add to `app/main.py` imports:

```python
from app.api.router_management import router as router_mgmt_router
from app.api.router_management import jump_host_router as jump_host_mgmt_router
```

Add to the route registration section:

```python
_include_api_router(router_mgmt_router, dependencies=[Depends(require_user_auth)])
_include_api_router(jump_host_mgmt_router, dependencies=[Depends(require_user_auth)])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_api.py -v`
Expected: All 8 tests PASS

- [ ] **Step 6: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/api/router_management.py app/main.py tests/test_router_management_api.py
git commit -m "feat(router-mgmt): add API endpoints and register routes"
```

---

## Task 9: Celery Tasks

**Files:**
- Create: `app/tasks/router_sync.py`

- [ ] **Step 1: Create Celery tasks**

Create `app/tasks/router_sync.py`:

```python
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.router_management import (
    Router,
    RouterConfigPush,
    RouterConfigPushStatus,
    RouterConfigPushResult,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterStatus,
)
from app.services.router_management.config import RouterConfigService
from app.services.router_management.connection import RouterConnectionService
from app.services.router_management.inventory import RouterInventory

logger = logging.getLogger(__name__)


@celery_app.task(name="router_sync.sync_all_system_info")
def sync_all_system_info() -> dict:
    db = SessionLocal()
    try:
        routers = list(
            db.execute(
                select(Router).where(Router.is_active.is_(True))
            ).scalars().all()
        )

        success = 0
        failed = 0
        for router in routers:
            try:
                sys_data = RouterConnectionService.execute(router, "GET", "/system/resource")
                rb_data = RouterConnectionService.execute(router, "GET", "/system/routerboard")

                router.routeros_version = sys_data.get("version")
                router.board_name = sys_data.get("board-name") or rb_data.get("model")
                router.architecture = sys_data.get("architecture-name")
                router.serial_number = rb_data.get("serial-number")
                router.firmware_type = rb_data.get("firmware-type")
                router.status = RouterStatus.online
                router.last_seen_at = datetime.now(timezone.utc)
                db.commit()
                success += 1
            except Exception as exc:
                logger.warning("Failed to sync %s: %s", router.name, exc)
                router.status = RouterStatus.unreachable
                db.commit()
                failed += 1

        return {"success": success, "failed": failed, "total": len(routers)}
    finally:
        db.close()


@celery_app.task(name="router_sync.sync_all_interfaces")
def sync_all_interfaces() -> dict:
    db = SessionLocal()
    try:
        routers = list(
            db.execute(
                select(Router).where(
                    Router.is_active.is_(True),
                    Router.status == RouterStatus.online,
                )
            ).scalars().all()
        )

        success = 0
        failed = 0
        for router in routers:
            try:
                iface_data = RouterConnectionService.execute(router, "GET", "/interface")
                if isinstance(iface_data, list):
                    interfaces = [
                        {
                            "name": i.get("name", ""),
                            "type": i.get("type", "ether"),
                            "mac_address": i.get("mac-address"),
                            "is_running": i.get("running", "false") == "true",
                            "is_disabled": i.get("disabled", "false") == "true",
                            "rx_byte": int(i.get("rx-byte", 0)),
                            "tx_byte": int(i.get("tx-byte", 0)),
                            "rx_packet": int(i.get("rx-packet", 0)),
                            "tx_packet": int(i.get("tx-packet", 0)),
                            "last_link_up_time": i.get("last-link-up-time"),
                            "speed": i.get("actual-mtu"),
                            "comment": i.get("comment"),
                        }
                        for i in iface_data
                    ]
                    RouterInventory.upsert_interfaces(db, router, interfaces)
                success += 1
            except Exception as exc:
                logger.warning("Failed to sync interfaces for %s: %s", router.name, exc)
                failed += 1

        return {"success": success, "failed": failed, "total": len(routers)}
    finally:
        db.close()


@celery_app.task(name="router_sync.capture_scheduled_snapshots")
def capture_scheduled_snapshots() -> dict:
    db = SessionLocal()
    try:
        routers = list(
            db.execute(
                select(Router).where(
                    Router.is_active.is_(True),
                    Router.status == RouterStatus.online,
                )
            ).scalars().all()
        )

        success = 0
        failed = 0
        for router in routers:
            try:
                data = RouterConnectionService.execute(router, "GET", "/export")
                config_text = data if isinstance(data, str) else str(data)
                RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=config_text,
                    source=RouterSnapshotSource.scheduled,
                )
                router.last_config_sync_at = datetime.now(timezone.utc)
                db.commit()
                success += 1
            except Exception as exc:
                logger.warning("Failed to snapshot %s: %s", router.name, exc)
                failed += 1

        return {"success": success, "failed": failed, "total": len(routers)}
    finally:
        db.close()


@celery_app.task(name="router_sync.cleanup_idle_tunnels")
def cleanup_idle_tunnels() -> dict:
    closed = RouterConnectionService.cleanup_idle_tunnels()
    return {"closed": closed}


@celery_app.task(name="router_sync.execute_config_push")
def execute_config_push(push_id: str) -> dict:
    db = SessionLocal()
    try:
        push = db.get(RouterConfigPush, push_id)
        if not push:
            return {"error": "Push not found"}

        push.status = RouterConfigPushStatus.running
        db.commit()

        success_count = 0
        fail_count = 0

        for result in push.results:
            router = db.get(Router, result.router_id)
            if not router or not router.is_active:
                result.status = RouterPushResultStatus.skipped
                result.error_message = "Router inactive or not found"
                db.commit()
                continue

            start_time = time.time()
            try:
                pre_data = RouterConnectionService.execute(router, "GET", "/export")
                pre_text = pre_data if isinstance(pre_data, str) else str(pre_data)
                pre_snap = RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=pre_text,
                    source=RouterSnapshotSource.pre_change,
                )
                result.pre_snapshot_id = pre_snap.id
                db.commit()

                responses = []
                for cmd in push.commands:
                    parts = cmd.strip().split(" ", 1)
                    path = parts[0] if parts else cmd
                    resp = RouterConnectionService.execute(router, "POST", path)
                    responses.append(resp)

                post_data = RouterConnectionService.execute(router, "GET", "/export")
                post_text = post_data if isinstance(post_data, str) else str(post_data)
                post_snap = RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=post_text,
                    source=RouterSnapshotSource.post_change,
                )

                result.post_snapshot_id = post_snap.id
                result.response_data = responses
                result.status = RouterPushResultStatus.success
                result.duration_ms = int((time.time() - start_time) * 1000)
                router.last_config_change_at = datetime.now(timezone.utc)
                db.commit()
                success_count += 1

            except Exception as exc:
                result.status = RouterPushResultStatus.failed
                result.error_message = str(exc)[:500]
                result.duration_ms = int((time.time() - start_time) * 1000)
                db.commit()
                fail_count += 1
                logger.warning("Push to %s failed: %s", router.name, exc)

        if fail_count == 0:
            push.status = RouterConfigPushStatus.completed
        elif success_count == 0:
            push.status = RouterConfigPushStatus.failed
        else:
            push.status = RouterConfigPushStatus.partial_failure
        push.completed_at = datetime.now(timezone.utc)
        db.commit()

        return {
            "push_id": push_id,
            "status": push.status.value,
            "success": success_count,
            "failed": fail_count,
        }
    finally:
        db.close()
```

- [ ] **Step 2: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/tasks/router_sync.py
git commit -m "feat(router-mgmt): add Celery tasks for router sync and config push"
```

---

## Task 10: Admin Web Routes

**Files:**
- Create: `app/web/admin/network_routers.py`
- Modify: `app/main.py` (register web router)

- [ ] **Step 1: Create web routes**

Create `app/web/admin/network_routers.py`:

```python
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_permission
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)
from app.services.router_management.inventory import (
    JumpHostInventory,
    RouterInventory,
)
from app.services.router_management.monitoring import RouterMonitoringService
from app.web.admin.network import _base_context

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network/routers",
    tags=["web-admin-routers"],
    dependencies=[Depends(require_permission("router:read"))],
)


@router.get("", response_class=HTMLResponse)
def router_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["routers"] = RouterInventory.list(
        db, status=status, search=search, limit=limit, offset=offset
    )
    context["status_filter"] = status
    context["search"] = search or ""
    context["summary"] = RouterMonitoringService.get_dashboard_summary(db)
    return templates.TemplateResponse("admin/network/routers/index.html", context)


@router.get("/dashboard", response_class=HTMLResponse)
def router_dashboard(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["summary"] = RouterMonitoringService.get_dashboard_summary(db)
    context["recent_pushes"] = RouterConfigService.list_pushes(db, limit=10)
    return templates.TemplateResponse("admin/network/routers/dashboard.html", context)


@router.get("/new", response_class=HTMLResponse)
def router_create_form(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["jump_hosts"] = JumpHostInventory.list(db)
    context["router"] = None
    return templates.TemplateResponse("admin/network/routers/form.html", context)


@router.post("/new", response_class=HTMLResponse)
def router_create(
    request: Request,
    db: Session = Depends(get_db),
):
    from app.schemas.router_management import RouterCreate
    from app.web.admin.network import parse_form_data_sync

    data = parse_form_data_sync(request)
    payload = RouterCreate(**data)
    r = RouterInventory.create(db, payload)
    return RedirectResponse(
        url=f"/admin/network/routers/{r.id}", status_code=303
    )


@router.get("/{router_id}", response_class=HTMLResponse)
def router_detail(
    request: Request,
    router_id: uuid.UUID,
    tab: str = "overview",
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    r = RouterInventory.get(db, router_id)
    context["router"] = r
    context["tab"] = tab

    if tab == "interfaces":
        from sqlalchemy import select
        from app.models.router_management import RouterInterface

        context["interfaces"] = list(
            db.execute(
                select(RouterInterface)
                .where(RouterInterface.router_id == router_id)
                .order_by(RouterInterface.name)
            ).scalars().all()
        )
    elif tab == "config":
        context["snapshots"] = RouterConfigService.list_snapshots(
            db, router_id, limit=20
        )
    elif tab == "pushes":
        from sqlalchemy import select
        from app.models.router_management import RouterConfigPushResult

        context["push_results"] = list(
            db.execute(
                select(RouterConfigPushResult)
                .where(RouterConfigPushResult.router_id == router_id)
                .order_by(RouterConfigPushResult.created_at.desc())
                .limit(20)
            ).scalars().all()
        )

    return templates.TemplateResponse("admin/network/routers/detail.html", context)


@router.get("/{router_id}/edit", response_class=HTMLResponse)
def router_edit_form(
    request: Request,
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["router"] = RouterInventory.get(db, router_id)
    context["jump_hosts"] = JumpHostInventory.list(db)
    return templates.TemplateResponse("admin/network/routers/form.html", context)


@router.post("/{router_id}/edit", response_class=HTMLResponse)
def router_edit(
    request: Request,
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    from app.schemas.router_management import RouterUpdate
    from app.web.admin.network import parse_form_data_sync

    data = parse_form_data_sync(request)
    payload = RouterUpdate(**data)
    RouterInventory.update(db, router_id, payload)
    return RedirectResponse(
        url=f"/admin/network/routers/{router_id}", status_code=303
    )


@router.get("/templates", response_class=HTMLResponse)
def template_list(
    request: Request,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["templates"] = RouterTemplateService.list(db, category=category)
    context["category_filter"] = category
    return templates.TemplateResponse(
        "admin/network/routers/templates/index.html", context
    )


@router.get("/templates/new", response_class=HTMLResponse)
def template_create_form(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["template"] = None
    return templates.TemplateResponse(
        "admin/network/routers/templates/form.html", context
    )


@router.get("/push", response_class=HTMLResponse)
def push_wizard(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["routers"] = RouterInventory.list(db, limit=200)
    context["templates"] = RouterTemplateService.list(db)
    return templates.TemplateResponse("admin/network/routers/push.html", context)


@router.get("/push/{push_id}", response_class=HTMLResponse)
def push_detail(
    request: Request,
    push_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    push = RouterConfigService.get_push(db, push_id)
    context["push"] = push
    context["results"] = push.results
    return templates.TemplateResponse(
        "admin/network/routers/push_detail.html", context
    )


@router.get("/jump-hosts", response_class=HTMLResponse)
def jump_host_list(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, "routers")
    context["jump_hosts"] = JumpHostInventory.list(db)
    return templates.TemplateResponse(
        "admin/network/routers/jump_hosts.html", context
    )
```

- [ ] **Step 2: Register web router in app/main.py**

Add import:

```python
from app.web.admin.network_routers import router as web_router_mgmt
```

Add registration (in the web router section):

```python
app.include_router(web_router_mgmt, prefix="/admin")
```

- [ ] **Step 3: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add app/web/admin/network_routers.py app/main.py
git commit -m "feat(router-mgmt): add admin web routes for router management"
```

---

## Task 11: Admin Templates

**Files:**
- Create: `templates/admin/network/routers/index.html`
- Create: `templates/admin/network/routers/dashboard.html`
- Create: `templates/admin/network/routers/detail.html`
- Create: `templates/admin/network/routers/form.html`
- Create: `templates/admin/network/routers/templates/index.html`
- Create: `templates/admin/network/routers/templates/form.html`
- Create: `templates/admin/network/routers/push.html`
- Create: `templates/admin/network/routers/push_detail.html`
- Create: `templates/admin/network/routers/jump_hosts.html`

This task creates all 9 Jinja2 templates. Each follows existing dotmac_sub conventions: extends `layouts/admin.html`, uses macros from `components/ui/macros.html`, Tailwind CSS with dark mode, HTMX partials, Alpine.js interactivity.

Due to the volume of templates, the implementation agent should:

- [ ] **Step 1: Create template directories**

```bash
mkdir -p templates/admin/network/routers/templates
```

- [ ] **Step 2: Create router list page** (`templates/admin/network/routers/index.html`)

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card, status_badge, empty_state %}

{% block content %}
{{ ambient_background("blue", "indigo") }}
<div class="relative space-y-8">
    {% call page_header(title="Routers", icon="router") %}
    <div class="flex gap-3">
        <a href="/admin/network/routers/dashboard"
           class="px-4 py-2 text-sm bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-600">
            Dashboard
        </a>
        <a href="/admin/network/routers/new"
           class="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">
            + Add Router
        </a>
    </div>
    {% endcall %}

    {# Summary cards #}
    <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
        {% for key, label, color in [
            ('online', 'Online', 'emerald'),
            ('offline', 'Offline', 'slate'),
            ('degraded', 'Degraded', 'amber'),
            ('maintenance', 'Maintenance', 'blue'),
            ('unreachable', 'Unreachable', 'red'),
        ] %}
        <div class="bg-white dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 p-4 text-center">
            <div class="text-2xl font-bold text-{{ color }}-600 dark:text-{{ color }}-400">{{ summary.get(key, 0) }}</div>
            <div class="text-sm text-slate-500 dark:text-slate-400">{{ label }}</div>
        </div>
        {% endfor %}
    </div>

    {# Filters #}
    <div class="flex gap-4 items-center">
        <form method="get" class="flex gap-3 items-center">
            <input type="text" name="search" value="{{ search }}" placeholder="Search routers..."
                   class="px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
            <select name="status" class="px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
                <option value="">All statuses</option>
                {% for s in ['online', 'offline', 'degraded', 'maintenance', 'unreachable'] %}
                <option value="{{ s }}" {{ 'selected' if status_filter == s }}>{{ s | capitalize }}</option>
                {% endfor %}
            </select>
            <button type="submit" class="px-4 py-2 text-sm bg-slate-100 dark:bg-slate-700 rounded-lg hover:bg-slate-200 dark:hover:bg-slate-600">Filter</button>
        </form>
    </div>

    {# Router table #}
    {% call card("Routers (" ~ summary.get('total', 0) ~ ")", color="blue") %}
    {% if routers %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-slate-200 dark:border-slate-700 text-left text-slate-500 dark:text-slate-400">
                    <th class="pb-3 font-medium">Name</th>
                    <th class="pb-3 font-medium">IP</th>
                    <th class="pb-3 font-medium">Version</th>
                    <th class="pb-3 font-medium">Status</th>
                    <th class="pb-3 font-medium">Access</th>
                    <th class="pb-3 font-medium">Location</th>
                    <th class="pb-3 font-medium">Last Seen</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-700">
                {% for r in routers %}
                <tr class="hover:bg-slate-50 dark:hover:bg-slate-700/50">
                    <td class="py-3">
                        <a href="/admin/network/routers/{{ r.id }}" class="text-blue-600 dark:text-blue-400 hover:underline font-medium">{{ r.name }}</a>
                    </td>
                    <td class="py-3 font-mono text-xs">{{ r.management_ip }}</td>
                    <td class="py-3">{{ r.routeros_version or '—' }}</td>
                    <td class="py-3">
                        {% set status_colors = {'online': 'emerald', 'offline': 'slate', 'degraded': 'amber', 'maintenance': 'blue', 'unreachable': 'red'} %}
                        {{ status_badge(r.status.value, status_colors.get(r.status.value, 'slate')) }}
                    </td>
                    <td class="py-3">{{ r.access_method.value }}</td>
                    <td class="py-3">{{ r.location or '—' }}</td>
                    <td class="py-3 text-slate-500">{{ r.last_seen_at.strftime('%Y-%m-%d %H:%M') if r.last_seen_at else '—' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    {{ empty_state("No routers found", "Add your first router to get started.") }}
    {% endif %}
    {% endcall %}
</div>
{% endblock %}
```

- [ ] **Step 3: Create dashboard page** (`templates/admin/network/routers/dashboard.html`)

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card, empty_state %}

{% block content %}
{{ ambient_background("indigo", "purple") }}
<div class="relative space-y-8">
    {% call page_header(title="Router Dashboard", icon="router") %}
    <a href="/admin/network/routers"
       class="px-4 py-2 text-sm bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-600">
        View All Routers
    </a>
    {% endcall %}

    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        <div class="bg-white dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 p-6 text-center">
            <div class="text-3xl font-bold text-slate-900 dark:text-white">{{ summary.total }}</div>
            <div class="text-sm text-slate-500 dark:text-slate-400 mt-1">Total</div>
        </div>
        {% for key, label, color in [
            ('online', 'Online', 'emerald'),
            ('offline', 'Offline', 'slate'),
            ('degraded', 'Degraded', 'amber'),
            ('maintenance', 'Maintenance', 'blue'),
            ('unreachable', 'Unreachable', 'red'),
        ] %}
        <div class="bg-white dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 p-6 text-center">
            <div class="text-3xl font-bold text-{{ color }}-600 dark:text-{{ color }}-400">{{ summary.get(key, 0) }}</div>
            <div class="text-sm text-slate-500 dark:text-slate-400 mt-1">{{ label }}</div>
        </div>
        {% endfor %}
    </div>

    {% call card("Recent Config Pushes", color="indigo") %}
    {% if recent_pushes %}
    <div class="space-y-3">
        {% for push in recent_pushes %}
        <a href="/admin/network/routers/push/{{ push.id }}"
           class="block p-3 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700/50">
            <div class="flex justify-between items-center">
                <span class="text-sm font-medium">{{ push.commands | length }} command(s) to {{ push.results | length }} router(s)</span>
                <span class="text-xs text-slate-500">{{ push.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
            </div>
            <div class="text-xs mt-1 text-slate-500">Status: {{ push.status.value }}</div>
        </a>
        {% endfor %}
    </div>
    {% else %}
    {{ empty_state("No recent pushes") }}
    {% endif %}
    {% endcall %}

    <div class="flex gap-4">
        <a href="/admin/network/routers/push" class="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
            New Config Push
        </a>
        <a href="/admin/network/routers/templates" class="px-6 py-3 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-600 text-sm font-medium">
            Manage Templates
        </a>
        <a href="/admin/network/routers/jump-hosts" class="px-6 py-3 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-600 text-sm font-medium">
            Jump Hosts
        </a>
    </div>
</div>
{% endblock %}
```

- [ ] **Step 4: Create detail page** (`templates/admin/network/routers/detail.html`)

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card, status_badge, empty_state %}

{% block content %}
{{ ambient_background("blue", "cyan") }}
<div class="relative space-y-8">
    {% set status_colors = {'online': 'emerald', 'offline': 'slate', 'degraded': 'amber', 'maintenance': 'blue', 'unreachable': 'red'} %}
    {% call page_header(title=router.name, icon="router") %}
    <div class="flex gap-3 items-center">
        {{ status_badge(router.status.value, status_colors.get(router.status.value, 'slate')) }}
        <a href="/admin/network/routers/{{ router.id }}/edit"
           class="px-4 py-2 text-sm bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-600">
            Edit
        </a>
        <button hx-post="/api/v1/network/routers/{{ router.id }}/sync" hx-swap="none"
                class="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">
            Sync Now
        </button>
    </div>
    {% endcall %}

    {# Tabs #}
    <div class="flex gap-1 border-b border-slate-200 dark:border-slate-700">
        {% for t, label in [('overview', 'Overview'), ('interfaces', 'Interfaces'), ('config', 'Config Snapshots'), ('pushes', 'Push History')] %}
        <a href="?tab={{ t }}"
           class="px-4 py-2 text-sm {{ 'border-b-2 border-blue-600 text-blue-600 font-medium' if tab == t else 'text-slate-500 hover:text-slate-700 dark:hover:text-slate-300' }}">
            {{ label }}
        </a>
        {% endfor %}
    </div>

    {% if tab == 'overview' %}
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        {% call card("System Info", color="blue") %}
        <dl class="grid grid-cols-2 gap-3 text-sm">
            <dt class="text-slate-500 dark:text-slate-400">Hostname</dt><dd>{{ router.hostname }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">Management IP</dt><dd class="font-mono">{{ router.management_ip }}:{{ router.rest_api_port }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">RouterOS Version</dt><dd>{{ router.routeros_version or '—' }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">Board</dt><dd>{{ router.board_name or '—' }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">Architecture</dt><dd>{{ router.architecture or '—' }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">Serial</dt><dd>{{ router.serial_number or '—' }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">Location</dt><dd>{{ router.location or '—' }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">Access Method</dt><dd>{{ router.access_method.value }}</dd>
            <dt class="text-slate-500 dark:text-slate-400">Last Seen</dt><dd>{{ router.last_seen_at.strftime('%Y-%m-%d %H:%M') if router.last_seen_at else '—' }}</dd>
        </dl>
        {% endcall %}

        {% call card("Notes", color="slate") %}
        <p class="text-sm text-slate-600 dark:text-slate-300">{{ router.notes or 'No notes.' }}</p>
        {% endcall %}
    </div>

    {% elif tab == 'interfaces' %}
    {% call card("Interfaces", color="cyan") %}
    {% if interfaces %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-slate-200 dark:border-slate-700 text-left text-slate-500 dark:text-slate-400">
                    <th class="pb-3 font-medium">Name</th>
                    <th class="pb-3 font-medium">Type</th>
                    <th class="pb-3 font-medium">MAC</th>
                    <th class="pb-3 font-medium">Status</th>
                    <th class="pb-3 font-medium">RX</th>
                    <th class="pb-3 font-medium">TX</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-700">
                {% for i in interfaces %}
                <tr>
                    <td class="py-2 font-mono text-xs">{{ i.name }}</td>
                    <td class="py-2">{{ i.type }}</td>
                    <td class="py-2 font-mono text-xs">{{ i.mac_address or '—' }}</td>
                    <td class="py-2">
                        {% if i.is_running %}
                        {{ status_badge('up', 'emerald') }}
                        {% elif i.is_disabled %}
                        {{ status_badge('disabled', 'slate') }}
                        {% else %}
                        {{ status_badge('down', 'red') }}
                        {% endif %}
                    </td>
                    <td class="py-2 font-mono text-xs">{{ '{:,.0f}'.format(i.rx_byte / 1048576) }} MB</td>
                    <td class="py-2 font-mono text-xs">{{ '{:,.0f}'.format(i.tx_byte / 1048576) }} MB</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    {{ empty_state("No interfaces synced", "Click 'Sync Now' to fetch interface data.") }}
    {% endif %}
    {% endcall %}

    {% elif tab == 'config' %}
    {% call card("Config Snapshots", color="teal") %}
    {% if snapshots %}
    <div class="space-y-3">
        {% for snap in snapshots %}
        <details class="border border-slate-200 dark:border-slate-700 rounded-lg">
            <summary class="px-4 py-3 cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-700/50 text-sm">
                <span class="font-medium">{{ snap.source.value }}</span>
                <span class="text-slate-500 ml-2">{{ snap.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
                <span class="text-xs text-slate-400 ml-2 font-mono">{{ snap.config_hash[:12] }}</span>
            </summary>
            <pre class="px-4 py-3 text-xs overflow-x-auto bg-slate-50 dark:bg-slate-900 border-t border-slate-200 dark:border-slate-700">{{ snap.config_export }}</pre>
        </details>
        {% endfor %}
    </div>
    {% else %}
    {{ empty_state("No config snapshots yet") }}
    {% endif %}
    {% endcall %}

    {% elif tab == 'pushes' %}
    {% call card("Push History", color="purple") %}
    {% if push_results %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-slate-200 dark:border-slate-700 text-left text-slate-500 dark:text-slate-400">
                    <th class="pb-3 font-medium">Date</th>
                    <th class="pb-3 font-medium">Status</th>
                    <th class="pb-3 font-medium">Duration</th>
                    <th class="pb-3 font-medium">Error</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-700">
                {% for pr in push_results %}
                <tr>
                    <td class="py-2">{{ pr.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
                    <td class="py-2">
                        {% set pr_colors = {'success': 'emerald', 'failed': 'red', 'pending': 'amber', 'skipped': 'slate'} %}
                        {{ status_badge(pr.status.value, pr_colors.get(pr.status.value, 'slate')) }}
                    </td>
                    <td class="py-2">{{ (pr.duration_ms ~ 'ms') if pr.duration_ms else '—' }}</td>
                    <td class="py-2 text-xs text-red-500">{{ pr.error_message[:80] if pr.error_message else '—' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    {{ empty_state("No push history for this router") }}
    {% endif %}
    {% endcall %}
    {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 5: Create router form** (`templates/admin/network/routers/form.html`)

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card %}

{% block content %}
{{ ambient_background("blue", "indigo") }}
<div class="relative space-y-8">
    {% call page_header(title=('Edit ' ~ router.name) if router else 'Add Router', icon="router") %}{% endcall %}

    {% call card("Router Details", color="blue") %}
    <form method="post" class="space-y-6 max-w-2xl" x-data="{ accessMethod: '{{ router.access_method.value if router else 'direct' }}' }">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Name</label>
                <input type="text" name="name" value="{{ router.name if router else '' }}" required
                       class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Hostname</label>
                <input type="text" name="hostname" value="{{ router.hostname if router else '' }}" required
                       class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Management IP</label>
                <input type="text" name="management_ip" value="{{ router.management_ip if router else '' }}" required
                       class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">REST API Port</label>
                <input type="number" name="rest_api_port" value="{{ router.rest_api_port if router else 443 }}"
                       class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">API Username</label>
                <input type="text" name="rest_api_username" value="{{ router.rest_api_username if router else 'admin' }}" required
                       class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">API Password</label>
                <input type="password" name="rest_api_password" {{ 'required' if not router }}
                       placeholder="{{ '(unchanged)' if router else '' }}"
                       class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Access Method</label>
                <select name="access_method" x-model="accessMethod"
                        class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
                    <option value="direct">Direct</option>
                    <option value="jump_host">Via Jump Host</option>
                </select>
            </div>
            <div x-show="accessMethod === 'jump_host'">
                <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Jump Host</label>
                <select name="jump_host_id"
                        class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
                    <option value="">Select...</option>
                    {% for jh in jump_hosts %}
                    <option value="{{ jh.id }}" {{ 'selected' if router and router.jump_host_id == jh.id }}>{{ jh.name }} ({{ jh.hostname }})</option>
                    {% endfor %}
                </select>
            </div>
        </div>

        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Location</label>
            <input type="text" name="location" value="{{ router.location if router and router.location else '' }}"
                   class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Notes</label>
            <textarea name="notes" rows="3"
                      class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">{{ router.notes if router and router.notes else '' }}</textarea>
        </div>

        <div class="flex gap-3">
            <button type="submit" class="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
                {{ 'Update Router' if router else 'Create Router' }}
            </button>
            <a href="/admin/network/routers{{ ('/' ~ router.id) if router else '' }}"
               class="px-6 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-600 text-sm">
                Cancel
            </a>
        </div>
    </form>
    {% endcall %}
</div>
{% endblock %}
```

- [ ] **Step 6: Create push wizard** (`templates/admin/network/routers/push.html`)

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card, empty_state %}

{% block content %}
{{ ambient_background("purple", "indigo") }}
<div class="relative space-y-8" x-data="pushWizard()">
    {% call page_header(title="Bulk Config Push", icon="router") %}{% endcall %}

    {# Step 1: Commands #}
    {% call card("1. Commands", color="purple") %}
    <div class="space-y-4">
        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Template (optional)</label>
            <select x-model="selectedTemplate" @change="loadTemplate()"
                    class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
                <option value="">— Ad-hoc commands —</option>
                {% for t in templates %}
                <option value="{{ t.id }}" data-body="{{ t.template_body | e }}">{{ t.name }} ({{ t.category.value }})</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Commands (one per line)</label>
            <textarea x-model="commands" rows="6"
                      class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm font-mono"
                      placeholder="/queue simple set [find] queue=sfq/sfq"></textarea>
        </div>
    </div>
    {% endcall %}

    {# Step 2: Select routers #}
    {% call card("2. Target Routers", color="blue") %}
    {% if routers %}
    <div class="mb-3">
        <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" @change="toggleAll($event)" class="rounded border-slate-300">
            <span>Select all</span>
        </label>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2 max-h-64 overflow-y-auto">
        {% for r in routers %}
        <label class="flex items-center gap-2 p-2 rounded hover:bg-slate-50 dark:hover:bg-slate-700/50 text-sm">
            <input type="checkbox" value="{{ r.id }}" x-model="selectedRouters" class="rounded border-slate-300">
            <span>{{ r.name }}</span>
            <span class="text-xs text-slate-400 font-mono">{{ r.management_ip }}</span>
        </label>
        {% endfor %}
    </div>
    {% else %}
    {{ empty_state("No routers available") }}
    {% endif %}
    {% endcall %}

    {# Step 3: Confirm #}
    {% call card("3. Confirm & Execute", color="emerald") %}
    <div class="flex items-center justify-between">
        <div class="text-sm text-slate-600 dark:text-slate-300">
            <span x-text="commands.split('\\n').filter(l => l.trim()).length">0</span> command(s) to
            <span x-text="selectedRouters.length">0</span> router(s)
        </div>
        <button @click="executePush()"
                :disabled="!commands.trim() || selectedRouters.length === 0"
                class="px-6 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed">
            Execute Push
        </button>
    </div>
    {% endcall %}
</div>

<script>
function pushWizard() {
    return {
        selectedTemplate: '',
        commands: '',
        selectedRouters: [],
        toggleAll(event) {
            if (event.target.checked) {
                this.selectedRouters = [...document.querySelectorAll('input[type=checkbox][value]')].map(el => el.value);
            } else {
                this.selectedRouters = [];
            }
        },
        loadTemplate() {
            const opt = document.querySelector(`option[value="${this.selectedTemplate}"]`);
            if (opt && opt.dataset.body) {
                this.commands = opt.dataset.body;
            }
        },
        async executePush() {
            const cmds = this.commands.split('\n').filter(l => l.trim());
            const resp = await fetch('/api/v1/network/routers/config-pushes', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({commands: cmds, router_ids: this.selectedRouters})
            });
            const data = await resp.json();
            if (resp.ok) {
                window.location.href = `/admin/network/routers/push/${data.id}`;
            } else {
                alert(data.detail || 'Push failed');
            }
        }
    }
}
</script>
{% endblock %}
```

- [ ] **Step 7: Create push detail page** (`templates/admin/network/routers/push_detail.html`)

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card, status_badge, empty_state %}

{% block content %}
{{ ambient_background("purple", "indigo") }}
<div class="relative space-y-8">
    {% set push_colors = {'completed': 'emerald', 'partial_failure': 'amber', 'failed': 'red', 'pending': 'slate', 'running': 'blue', 'rolled_back': 'orange'} %}
    {% call page_header(title="Push Results", icon="router") %}
    {{ status_badge(push.status.value, push_colors.get(push.status.value, 'slate')) }}
    {% endcall %}

    {% call card("Commands", color="purple") %}
    <pre class="text-xs font-mono bg-slate-50 dark:bg-slate-900 p-3 rounded-lg overflow-x-auto">{% for cmd in push.commands %}{{ cmd }}
{% endfor %}</pre>
    <div class="mt-2 text-xs text-slate-500">
        Created: {{ push.created_at.strftime('%Y-%m-%d %H:%M') }}
        {% if push.completed_at %}| Completed: {{ push.completed_at.strftime('%Y-%m-%d %H:%M') }}{% endif %}
    </div>
    {% endcall %}

    {% call card("Per-Router Results", color="blue") %}
    {% if results %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-slate-200 dark:border-slate-700 text-left text-slate-500 dark:text-slate-400">
                    <th class="pb-3 font-medium">Router</th>
                    <th class="pb-3 font-medium">Status</th>
                    <th class="pb-3 font-medium">Duration</th>
                    <th class="pb-3 font-medium">Error</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-700">
                {% for r in results %}
                <tr>
                    <td class="py-3">
                        <a href="/admin/network/routers/{{ r.router_id }}" class="text-blue-600 dark:text-blue-400 hover:underline">
                            {{ r.router.name if r.router else r.router_id }}
                        </a>
                    </td>
                    <td class="py-3">
                        {% set r_colors = {'success': 'emerald', 'failed': 'red', 'pending': 'amber', 'skipped': 'slate'} %}
                        {{ status_badge(r.status.value, r_colors.get(r.status.value, 'slate')) }}
                    </td>
                    <td class="py-3">{{ (r.duration_ms ~ 'ms') if r.duration_ms else '—' }}</td>
                    <td class="py-3 text-xs text-red-500">{{ r.error_message or '—' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    {{ empty_state("No results yet") }}
    {% endif %}
    {% endcall %}
</div>
{% endblock %}
```

- [ ] **Step 8: Create remaining templates**

Create `templates/admin/network/routers/templates/index.html`:

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card, status_badge, empty_state %}

{% block content %}
{{ ambient_background("teal", "emerald") }}
<div class="relative space-y-8">
    {% call page_header(title="Config Templates", icon="router") %}
    <a href="/admin/network/routers/templates/new"
       class="px-4 py-2 text-sm bg-teal-600 text-white rounded-lg hover:bg-teal-700">
        + New Template
    </a>
    {% endcall %}

    {% call card("Templates", color="teal") %}
    {% if templates %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-slate-200 dark:border-slate-700 text-left text-slate-500 dark:text-slate-400">
                    <th class="pb-3 font-medium">Name</th>
                    <th class="pb-3 font-medium">Category</th>
                    <th class="pb-3 font-medium">Active</th>
                    <th class="pb-3 font-medium">Updated</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-700">
                {% for t in templates %}
                <tr class="hover:bg-slate-50 dark:hover:bg-slate-700/50">
                    <td class="py-3">
                        <a href="/admin/network/routers/templates/{{ t.id }}" class="text-teal-600 dark:text-teal-400 hover:underline font-medium">{{ t.name }}</a>
                        {% if t.description %}<div class="text-xs text-slate-500 mt-1">{{ t.description }}</div>{% endif %}
                    </td>
                    <td class="py-3">{{ t.category.value }}</td>
                    <td class="py-3">{{ status_badge('active', 'emerald') if t.is_active else status_badge('inactive', 'slate') }}</td>
                    <td class="py-3 text-slate-500">{{ t.updated_at.strftime('%Y-%m-%d') }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    {{ empty_state("No templates yet", "Create your first config template.") }}
    {% endif %}
    {% endcall %}
</div>
{% endblock %}
```

Create `templates/admin/network/routers/templates/form.html`:

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card %}

{% block content %}
{{ ambient_background("teal", "emerald") }}
<div class="relative space-y-8">
    {% call page_header(title=('Edit ' ~ template.name) if template else 'New Config Template', icon="router") %}{% endcall %}

    {% call card("Template Details", color="teal") %}
    <form method="post" class="space-y-6 max-w-2xl">
        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Name</label>
            <input type="text" name="name" value="{{ template.name if template else '' }}" required
                   class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Description</label>
            <input type="text" name="description" value="{{ template.description if template and template.description else '' }}"
                   class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Category</label>
            <select name="category" class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm">
                {% for cat in ['firewall', 'queue', 'address_list', 'routing', 'dns', 'ntp', 'snmp', 'system', 'custom'] %}
                <option value="{{ cat }}" {{ 'selected' if template and template.category.value == cat }}>{{ cat | replace('_', ' ') | capitalize }}</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Template Body (Jinja2)</label>
            <textarea name="template_body" rows="10" required
                      class="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-sm font-mono"
                      placeholder="/queue simple set [find] queue={{ '{{' }} queue_type {{ '}}' }}/{{ '{{' }} queue_type {{ '}}' }}">{{ template.template_body if template else '' }}</textarea>
            <p class="text-xs text-slate-500 mt-1">Use {{ '{{' }} variable_name {{ '}}' }} for template variables.</p>
        </div>
        <div class="flex gap-3">
            <button type="submit" class="px-6 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 text-sm font-medium">
                {{ 'Update' if template else 'Create Template' }}
            </button>
            <a href="/admin/network/routers/templates" class="px-6 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg text-sm">Cancel</a>
        </div>
    </form>
    {% endcall %}
</div>
{% endblock %}
```

Create `templates/admin/network/routers/jump_hosts.html`:

```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import ambient_background, page_header, card, status_badge, empty_state %}

{% block content %}
{{ ambient_background("slate", "zinc") }}
<div class="relative space-y-8">
    {% call page_header(title="Jump Hosts", icon="router") %}{% endcall %}

    {% call card("SSH Jump Hosts", color="slate") %}
    {% if jump_hosts %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-slate-200 dark:border-slate-700 text-left text-slate-500 dark:text-slate-400">
                    <th class="pb-3 font-medium">Name</th>
                    <th class="pb-3 font-medium">Hostname</th>
                    <th class="pb-3 font-medium">Port</th>
                    <th class="pb-3 font-medium">Username</th>
                    <th class="pb-3 font-medium">Status</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-700">
                {% for jh in jump_hosts %}
                <tr>
                    <td class="py-3 font-medium">{{ jh.name }}</td>
                    <td class="py-3 font-mono text-xs">{{ jh.hostname }}</td>
                    <td class="py-3">{{ jh.port }}</td>
                    <td class="py-3">{{ jh.username }}</td>
                    <td class="py-3">{{ status_badge('active', 'emerald') if jh.is_active else status_badge('inactive', 'slate') }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    {{ empty_state("No jump hosts configured") }}
    {% endif %}
    {% endcall %}
</div>
{% endblock %}
```

- [ ] **Step 9: Commit**

```bash
cd /home/dotmac/projects/dotmac_sub
git add templates/admin/network/routers/
git commit -m "feat(router-mgmt): add admin UI templates for router management"
```

---

## Task 12: Lint, Type Check, and Full Test Suite

- [ ] **Step 1: Run ruff lint**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run ruff check app/models/router_management.py app/schemas/router_management.py app/services/router_management/ app/api/router_management.py app/web/admin/network_routers.py app/tasks/router_sync.py`

Fix any issues found.

- [ ] **Step 2: Run ruff format**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run ruff format app/models/router_management.py app/schemas/router_management.py app/services/router_management/ app/api/router_management.py app/web/admin/network_routers.py app/tasks/router_sync.py`

- [ ] **Step 3: Run mypy**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run mypy app/models/router_management.py app/schemas/router_management.py app/services/router_management/ app/api/router_management.py`

Fix any type errors.

- [ ] **Step 4: Run full test suite**

Run: `cd /home/dotmac/projects/dotmac_sub && poetry run pytest tests/test_router_management_*.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit any fixes**

```bash
cd /home/dotmac/projects/dotmac_sub
git add -A
git commit -m "fix(router-mgmt): lint, format, and type check fixes"
```

- [ ] **Step 6: Final commit — all router management code**

Verify everything is committed:

```bash
cd /home/dotmac/projects/dotmac_sub && git status
```
