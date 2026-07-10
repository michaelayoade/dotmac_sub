import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class FieldVendor(Base):
    __tablename__ = "field_vendors"
    __table_args__ = (
        UniqueConstraint("code", name="uq_field_vendors_code"),
        UniqueConstraint("crm_vendor_id", name="uq_field_vendors_crm_vendor_id"),
        Index("ix_field_vendors_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_vendor_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60))
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    service_area: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    users = relationship("FieldVendorUser", back_populates="vendor")


class FieldVendorUser(Base):
    __tablename__ = "field_vendor_users"
    __table_args__ = (
        UniqueConstraint(
            "vendor_id",
            "system_user_id",
            name="uq_field_vendor_users_vendor_system_user",
        ),
        UniqueConstraint(
            "crm_vendor_user_id", name="uq_field_vendor_users_crm_vendor_user_id"
        ),
        Index("ix_field_vendor_users_system_user_id", "system_user_id"),
        Index("ix_field_vendor_users_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_vendor_user_id: Mapped[str | None] = mapped_column(String(64))
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_vendors.id", ondelete="CASCADE"),
        nullable=False,
    )
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    vendor = relationship("FieldVendor", back_populates="users")
    system_user = relationship("SystemUser")


class FieldVendorDeviceToken(Base):
    __tablename__ = "field_vendor_device_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_field_vendor_device_tokens_token"),
        Index("ix_field_vendor_device_tokens_vendor_user_id", "vendor_user_id"),
        Index("ix_field_vendor_device_tokens_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_vendor_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(16))
    app_version: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    vendor_user = relationship("FieldVendorUser")

    @property
    def subscriber_id(self):
        return None

    @property
    def system_user_id(self):
        return None
