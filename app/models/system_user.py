import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.subscriber import UserType

SystemUserType = UserType


class SystemUser(Base):
    """Staff authentication principal with optional canonical Person identity."""

    __tablename__ = "system_users"
    __table_args__ = (
        CheckConstraint(
            "(person_party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(person_party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_system_users_party_binding_evidence",
        ),
        UniqueConstraint("person_party_id", name="uq_system_users_person_party_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    user_type: Mapped[UserType] = mapped_column(
        Enum(UserType), default=UserType.system_user, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    device_login_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false(), nullable=False
    )
    device_login_secret: Mapped[str | None] = mapped_column(String(512))
    device_login_secret_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    device_login_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    person_party = relationship("Party", back_populates="system_user_profile")
