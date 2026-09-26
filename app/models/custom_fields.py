"""Governed custom-field definitions and tenant-scoped entity values."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CustomFieldType(StrEnum):
    text = "text"
    textarea = "textarea"
    integer = "integer"
    decimal = "decimal"
    boolean = "boolean"
    date = "date"
    datetime = "datetime"
    select = "select"
    multiselect = "multiselect"
    email = "email"
    url = "url"
    phone = "phone"
    currency = "currency"


class CustomFieldDefinitionStatus(StrEnum):
    draft = "draft"
    active = "active"
    retired = "retired"


class CustomFieldDefinition(Base):
    __tablename__ = "custom_field_definitions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "target_type",
            "key",
            name="uq_custom_field_definitions_tenant_target_key",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_custom_field_definitions_tenant_id"
        ),
        CheckConstraint("length(trim(target_type)) > 0", name="ck_custom_field_target"),
        CheckConstraint("length(trim(key)) > 0", name="ck_custom_field_key"),
        CheckConstraint("length(trim(label)) > 0", name="ck_custom_field_label"),
        CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_custom_field_definition_status",
        ),
        CheckConstraint(
            "field_type IN ('text', 'textarea', 'integer', 'decimal', 'boolean', "
            "'date', 'datetime', 'select', 'multiselect', 'email', 'url', "
            "'phone', 'currency')",
            name="ck_custom_field_definition_type",
        ),
        CheckConstraint("display_order >= 0", name="ck_custom_field_display_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Tenant is kernel-owned on separate SQLAlchemy metadata. Migration 586
    # remains the database authority for this foreign key.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    field_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=CustomFieldDefinitionStatus.draft.value
    )
    options: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    validation: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    default_value: Mapped[object | None] = mapped_column(JSONB)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    section: Mapped[str] = mapped_column(
        String(120), nullable=False, default="Additional information"
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    show_in_list: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    show_in_form: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_in_detail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomFieldValue(Base):
    __tablename__ = "custom_field_values"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "definition_id",
            "target_id",
            name="uq_custom_field_values_tenant_definition_target",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["custom_field_definitions.tenant_id", "custom_field_definitions.id"],
            ondelete="CASCADE",
            name="fk_custom_field_values_definition_tenant",
        ),
        CheckConstraint("length(trim(target_type)) > 0", name="ck_custom_value_target"),
        Index(
            "ix_custom_field_values_target",
            "tenant_id",
            "target_type",
            "target_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    value: Mapped[object] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


__all__ = [
    "CustomFieldDefinition",
    "CustomFieldDefinitionStatus",
    "CustomFieldType",
    "CustomFieldValue",
]
