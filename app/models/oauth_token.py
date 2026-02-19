"""OAuth Token model for storing Meta (Facebook/Instagram) and other OAuth tokens."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OAuthToken(Base):
    """Stores OAuth access tokens for external integrations.

    This model supports multiple accounts per connector (e.g., multiple Facebook Pages
    or Instagram Business accounts connected to a single Meta connector).

    Attributes:
        provider: The OAuth provider (e.g., "meta", "google")
        account_type: Type of account (e.g., "page", "instagram_business")
        external_account_id: The provider's ID for this account (Page ID, IG account ID)
        external_account_name: Display name for the account
        access_token: The OAuth access token (may be Vault reference)
        token_expires_at: When the token expires
        scopes: List of granted OAuth scopes
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        UniqueConstraint(
            "connector_config_id",
            "provider",
            "external_account_id",
            name="uq_oauth_tokens_connector_provider_account",
        ),
        Index("ix_oauth_tokens_connector_config_id", "connector_config_id"),
        Index("ix_oauth_tokens_token_expires_at", "token_expires_at"),
        Index("ix_oauth_tokens_provider", "provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connector_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id"), nullable=False
    )

    # Provider identification
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    account_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    external_account_name: Mapped[str | None] = mapped_column(String(255))

    # Token storage (may be Vault reference like "vault://path#field")
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    token_type: Mapped[str | None] = mapped_column(String(64))
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Token metadata
    scopes: Mapped[list | None] = mapped_column(JSON)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_error: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    connector_config = relationship("ConnectorConfig")

    def is_token_expired(self) -> bool:
        """Check if the access token has expired."""
        if not self.token_expires_at:
            return False
        return datetime.now(UTC) >= self.token_expires_at

    def should_refresh(self, buffer_days: int = 7) -> bool:
        """Check if token should be proactively refreshed.

        Args:
            buffer_days: Number of days before expiry to trigger refresh.
                        Default is 7 days for Meta's 60-day tokens.

        Returns:
            True if token is within buffer period of expiring.
        """
        if not self.token_expires_at:
            return False
        buffer = timedelta(days=buffer_days)
        return datetime.now(UTC) >= (self.token_expires_at - buffer)

    def days_until_expiry(self) -> int | None:
        """Return number of days until token expires, or None if no expiry set."""
        if not self.token_expires_at:
            return None
        delta = self.token_expires_at - datetime.now(UTC)
        return max(0, delta.days)

    def __repr__(self) -> str:
        return (
            f"<OAuthToken(id={self.id}, provider={self.provider}, "
            f"account_type={self.account_type}, external_account_id={self.external_account_id})>"
        )
