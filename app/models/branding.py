from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BrandProfile(Base):
    """Canonical customer-facing identity for a platform or tenant scope."""

    __tablename__ = "brand_profiles"
    __table_args__ = (
        CheckConstraint(
            "(scope_type = 'platform' AND scope_id IS NULL) OR "
            "(scope_type IN ('reseller', 'organization') AND scope_id IS NOT NULL)",
            name="ck_brand_profiles_scope",
        ),
        Index(
            "uq_brand_profiles_platform",
            "scope_type",
            unique=True,
            postgresql_where=text("scope_id IS NULL"),
            sqlite_where=text("scope_id IS NULL"),
        ),
        Index(
            "uq_brand_profiles_scoped",
            "scope_type",
            "scope_id",
            unique=True,
            postgresql_where=text("scope_id IS NOT NULL"),
            sqlite_where=text("scope_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_type: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    brand_name: Mapped[str | None] = mapped_column(String(120))
    product_name: Mapped[str | None] = mapped_column(String(160))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    tagline: Mapped[str | None] = mapped_column(String(255))
    primary_color: Mapped[str | None] = mapped_column(String(7))
    secondary_color: Mapped[str | None] = mapped_column(String(7))
    logo_url: Mapped[str | None] = mapped_column(Text)
    dark_logo_url: Mapped[str | None] = mapped_column(Text)
    favicon_url: Mapped[str | None] = mapped_column(Text)
    support_email: Mapped[str | None] = mapped_column(String(255))
    support_phone: Mapped[str | None] = mapped_column(String(40))
    from_email: Mapped[str | None] = mapped_column(String(255))
    from_name: Mapped[str | None] = mapped_column(String(160))
    app_url: Mapped[str | None] = mapped_column(String(512))
    portal_domain: Mapped[str | None] = mapped_column(String(255))
    legal_address: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSON(none_as_null=True)
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
