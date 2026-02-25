import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db import Base


class AuthProvider(enum.Enum):
    local = "local"
    sso = "sso"
    radius = "radius"


class MFAMethodType(enum.Enum):
    totp = "totp"
    sms = "sms"
    email = "email"


class SessionStatus(enum.Enum):
    active = "active"
    revoked = "revoked"
    expired = "expired"


class UserCredential(Base):
    __tablename__ = "user_credentials"
    __table_args__ = (
        CheckConstraint(
            "(provider != 'local') OR (username IS NOT NULL AND password_hash IS NOT NULL)",
            name="ck_user_credentials_local_requires_username_password",
        ),
        CheckConstraint(
            "(subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)",
            name="ck_user_credentials_exactly_one_principal",
        ),
        Index(
            "ix_user_credentials_local_username_unique",
            "username",
            unique=True,
            postgresql_where=text("provider = 'local'"),
            sqlite_where=text("provider = 'local'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=True
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    provider: Mapped[AuthProvider] = mapped_column(
        Enum(AuthProvider), default=AuthProvider.local, nullable=False
    )
    username: Mapped[str | None] = mapped_column(String(150))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    radius_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_servers.id")
    )
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")
    radius_server = relationship("RadiusServer")


class MFAMethod(Base):
    __tablename__ = "mfa_methods"
    __table_args__ = (
        Index(
            "ix_mfa_methods_primary_per_subscriber",
            "subscriber_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
        Index(
            "ix_mfa_methods_primary_per_system_user",
            "system_user_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
        CheckConstraint(
            "(subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)",
            name="ck_mfa_methods_exactly_one_principal",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=True
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    method_type: Mapped[MFAMethodType] = mapped_column(
        Enum(MFAMethodType), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(120))
    secret: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "(subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)",
            name="ck_sessions_exactly_one_principal",
        ),
        Index("ux_sessions_token_hash", "token_hash", unique=True),
        Index(
            "ux_sessions_previous_token_hash",
            "previous_token_hash",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=True
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus), default=SessionStatus.active, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_token_hash: Mapped[str | None] = mapped_column(String(255))
    token_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID | None] = synonym("subscriber_id")
    label: Mapped[str | None] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")
