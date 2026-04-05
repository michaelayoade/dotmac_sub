"""Network operation tracking model.

Provides durable state tracking for long-running or multi-step network
device operations (OLT sync, ONT authorization, TR-069 bootstrap, etc.).
Operations are composable via parent/child relationships, allowing complex
workflows to be broken into independently trackable sub-operations.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class NetworkOperationStatus(enum.Enum):
    """Lifecycle status for a network operation."""

    pending = "pending"
    running = "running"
    waiting = "waiting"  # Waiting on external system (device inform, reboot)
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class NetworkOperationType(enum.Enum):
    """Types of network operations that can be tracked."""

    olt_ont_sync = "olt_ont_sync"
    olt_pon_repair = "olt_pon_repair"
    ont_authorize = "ont_authorize"
    ont_reboot = "ont_reboot"
    ont_factory_reset = "ont_factory_reset"
    ont_set_pppoe = "ont_set_pppoe"
    ont_set_conn_request_creds = "ont_set_conn_request_creds"
    ont_send_conn_request = "ont_send_conn_request"
    ont_enable_ipv6 = "ont_enable_ipv6"
    cpe_set_conn_request_creds = "cpe_set_conn_request_creds"
    cpe_send_conn_request = "cpe_send_conn_request"
    cpe_reboot = "cpe_reboot"
    cpe_factory_reset = "cpe_factory_reset"
    tr069_bootstrap = "tr069_bootstrap"
    wifi_update = "wifi_update"
    pppoe_push = "pppoe_push"
    router_config_push = "router_config_push"
    router_config_backup = "router_config_backup"
    router_reboot = "router_reboot"
    router_firmware_upgrade = "router_firmware_upgrade"
    router_bulk_push = "router_bulk_push"


class NetworkOperationTargetType(enum.Enum):
    """Target device type for a network operation."""

    olt = "olt"
    ont = "ont"
    cpe = "cpe"


class NetworkOperation(Base):
    """A tracked network device operation.

    Records the lifecycle of a network action from initiation through
    completion, including input parameters, results, errors, and retry
    state. Supports parent/child composition for multi-step workflows.

    Invariants enforced at DB level:
    - Only one active operation per correlation_key (partial unique index).
    """

    __tablename__ = "network_operations"
    __table_args__ = (
        Index(
            "ix_netops_target",
            "target_type",
            "target_id",
        ),
        Index(
            "ix_netops_status",
            "status",
        ),
        Index(
            "ix_netops_parent",
            "parent_id",
        ),
        # Partial unique index on correlation_key is created in the Alembic
        # migration via raw SQL (PostgreSQL-only feature). The service layer
        # enforces the same constraint in application code for portability.
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    operation_type: Mapped[NetworkOperationType] = mapped_column(
        Enum(
            NetworkOperationType,
            name="networkoperationtype",
            create_constraint=False,
        ),
        nullable=False,
    )
    target_type: Mapped[NetworkOperationTargetType] = mapped_column(
        Enum(
            NetworkOperationTargetType,
            name="networkoperationtargettype",
            create_constraint=False,
        ),
        nullable=False,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="CASCADE"),
        nullable=True,
    )
    status: Mapped[NetworkOperationStatus] = mapped_column(
        Enum(
            NetworkOperationStatus,
            name="networkoperationstatus",
            create_constraint=False,
        ),
        default=NetworkOperationStatus.pending,
    )
    correlation_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    initiated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    parent: Mapped[NetworkOperation | None] = relationship(
        "NetworkOperation",
        remote_side=[id],
        back_populates="children",
    )
    children: Mapped[list[NetworkOperation]] = relationship(
        "NetworkOperation",
        back_populates="parent",
    )
