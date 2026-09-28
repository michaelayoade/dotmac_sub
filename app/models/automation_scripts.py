"""Native Automation Center client and server script definitions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AutomationScriptKind(StrEnum):
    client = "client_script"
    server = "server_script"


class AutomationScriptLanguage(StrEnum):
    javascript = "javascript"


class AutomationScriptStatus(StrEnum):
    draft = "draft"
    published = "published"
    paused = "paused"
    retired = "retired"


class AutomationScriptRunStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    blocked = "blocked"
    reconciliation_required = "reconciliation_required"


class AutomationScript(Base):
    """Tenant-scoped script identity and lifecycle state.

    Source code belongs to an immutable version row. The identity row only
    selects the currently published version and never stores executable text.
    """

    __tablename__ = "automation_scripts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_automation_scripts_tenant_key"),
        UniqueConstraint("tenant_id", "id", name="uq_automation_scripts_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "active_version_id"],
            ["automation_script_versions.tenant_id", "automation_script_versions.id"],
            name="fk_automation_scripts_active_version_tenant",
            use_alter=True,
        ),
        CheckConstraint(
            "kind IN ('client_script', 'server_script')",
            name="ck_automation_scripts_kind",
        ),
        CheckConstraint(
            "language IN ('javascript')",
            name="ck_automation_scripts_language",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'paused', 'retired')",
            name="ck_automation_scripts_status",
        ),
        CheckConstraint(
            "length(trim(key)) > 0",
            name="ck_automation_scripts_key",
        ),
        CheckConstraint(
            "length(trim(target_type)) > 0 AND length(trim(event_name)) > 0",
            name="ck_automation_scripts_target_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    language: Mapped[str] = mapped_column(String(24), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    event_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=AutomationScriptStatus.draft.value
    )
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AutomationScriptVersion(Base):
    """Immutable code and execution policy for one script revision."""

    __tablename__ = "automation_script_versions"
    __table_args__ = (
        UniqueConstraint(
            "script_id", "version", name="uq_automation_script_versions_revision"
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_automation_script_versions_tenant_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "script_id"],
            ["automation_scripts.tenant_id", "automation_scripts.id"],
            ondelete="CASCADE",
            name="fk_automation_script_versions_script_tenant",
        ),
        Index(
            "uq_automation_script_versions_draft",
            "script_id",
            unique=True,
            postgresql_where=text("published_at IS NULL"),
            sqlite_where=text("published_at IS NULL"),
        ),
        CheckConstraint("version >= 1", name="ck_automation_script_versions_version"),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_automation_script_versions_hash",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    script_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    api_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    limits: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    published_by: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationScriptRun(Base):
    """Redacted evidence for one server-script invocation."""

    __tablename__ = "automation_script_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_automation_script_runs_tenant_id"),
        UniqueConstraint(
            "script_version_id", "event_id", name="uq_automation_script_runs_event"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "script_id"],
            ["automation_scripts.tenant_id", "automation_scripts.id"],
            name="fk_automation_script_runs_script_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "script_version_id"],
            ["automation_script_versions.tenant_id", "automation_script_versions.id"],
            name="fk_automation_script_runs_version_tenant",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'blocked', 'reconciliation_required')",
            name="ck_automation_script_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    script_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    script_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AutomationScriptRunStatus.pending.value
    )
    result_code: Mapped[str | None] = mapped_column(String(160))
    error_code: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
