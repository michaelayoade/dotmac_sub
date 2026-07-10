import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InboxChannelType(enum.Enum):
    email = "email"
    whatsapp = "whatsapp"
    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"
    chat_widget = "chat_widget"
    note = "note"


class InboxConversationStatus(enum.Enum):
    open = "open"
    pending = "pending"
    snoozed = "snoozed"
    resolved = "resolved"


class InboxMessageDirection(enum.Enum):
    inbound = "inbound"
    outbound = "outbound"
    internal = "internal"


class InboxAgentPresenceStatus(enum.Enum):
    online = "online"
    away = "away"
    on_break = "on_break"
    offline = "offline"


class InboxTeamRole(enum.Enum):
    owner = "owner"
    participant = "participant"
    watcher = "watcher"


class InboxTeamSource(enum.Enum):
    recipient_to = "recipient_to"
    recipient_cc = "recipient_cc"
    routing_rule = "routing_rule"
    escalation = "escalation"
    manual = "manual"


class TeamInboxEmailRoute(Base):
    __tablename__ = "team_inbox_email_routes"
    __table_args__ = (
        UniqueConstraint(
            "service_team_id",
            "email_address",
            name="uq_team_inbox_email_routes_team_address",
        ),
        Index(
            "ix_team_inbox_email_routes_address_active", "email_address", "is_active"
        ),
        Index("ix_team_inbox_email_routes_team", "service_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    email_address: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    service_team = relationship("ServiceTeam")


class InboxConversation(Base):
    __tablename__ = "inbox_conversations"
    __table_args__ = (
        Index("ix_inbox_conversations_subscriber", "subscriber_id"),
        Index("ix_inbox_conversations_primary_team", "primary_service_team_id"),
        Index("ix_inbox_conversations_status_last", "status", "last_message_at"),
        Index(
            "ix_inbox_conversations_external_thread",
            "channel_type",
            "external_thread_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    primary_service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    channel_type: Mapped[str] = mapped_column(
        String(40), default=InboxChannelType.email.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(40), default=InboxConversationStatus.open.value, nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(200))
    contact_address: Mapped[str | None] = mapped_column(String(255))
    external_thread_id: Mapped[str | None] = mapped_column(String(255))
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    primary_service_team = relationship("ServiceTeam")
    team_links = relationship(
        "InboxConversationTeam",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    messages = relationship(
        "InboxMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    assignments = relationship(
        "InboxConversationAssignment",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class InboxContactLink(Base):
    __tablename__ = "inbox_contact_links"
    __table_args__ = (
        CheckConstraint(
            "(subscriber_id IS NOT NULL AND reseller_id IS NULL)"
            " OR (subscriber_id IS NULL AND reseller_id IS NOT NULL)",
            name="ck_inbox_contact_links_one_target",
        ),
        Index(
            "ix_inbox_contact_links_contact",
            "channel_type",
            "normalized_contact",
            "is_active",
        ),
        Index("ix_inbox_contact_links_subscriber", "subscriber_id", "is_active"),
        Index("ix_inbox_contact_links_reseller", "reseller_id", "is_active"),
        Index(
            "uq_inbox_contact_links_active_contact",
            "channel_type",
            "normalized_contact",
            unique=True,
            sqlite_where=text("is_active IS TRUE"),
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_contact: Mapped[str] = mapped_column(String(255), nullable=False)
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )
    linked_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    reseller = relationship("Reseller")


class InboxConversationTeam(Base):
    __tablename__ = "inbox_conversation_teams"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "service_team_id",
            name="uq_inbox_conversation_teams_conversation_team",
        ),
        Index("ix_inbox_conversation_teams_team_role", "service_team_id", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(40), default=InboxTeamRole.participant.value, nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(40), default=InboxTeamSource.routing_rule.value, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation", back_populates="team_links")
    service_team = relationship("ServiceTeam")


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        Index("ix_inbox_messages_conversation", "conversation_id", "created_at"),
        Index(
            "uq_inbox_messages_inbound_external",
            "channel_type",
            "external_message_id",
            unique=True,
            sqlite_where=text(
                "external_message_id IS NOT NULL AND direction = 'inbound'"
            ),
            postgresql_where=text(
                "external_message_id IS NOT NULL AND direction = 'inbound'"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(
        String(40), default=InboxChannelType.email.value, nullable=False
    )
    direction: Mapped[str] = mapped_column(
        String(40), default=InboxMessageDirection.inbound.value, nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    external_message_id: Mapped[str | None] = mapped_column(String(255))
    external_thread_id: Mapped[str | None] = mapped_column(String(255))
    from_address: Mapped[str | None] = mapped_column(String(255))
    to_addresses: Mapped[list | None] = mapped_column(JSON)
    cc_addresses: Mapped[list | None] = mapped_column(JSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation", back_populates="messages")


class InboxAgentPresence(Base):
    __tablename__ = "inbox_agent_presence"
    __table_args__ = (
        UniqueConstraint("person_id", name="uq_inbox_agent_presence_person"),
        Index("ix_inbox_agent_presence_status", "status"),
        Index("ix_inbox_agent_presence_last_seen_at", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=InboxAgentPresenceStatus.offline.value, nullable=False
    )
    manual_override_status: Mapped[str | None] = mapped_column(String(40))
    max_concurrent_conversations: Mapped[int | None] = mapped_column(Integer)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class InboxConversationAssignment(Base):
    __tablename__ = "inbox_conversation_assignments"
    __table_args__ = (
        Index(
            "uq_inbox_conversation_one_active_assignment",
            "conversation_id",
            unique=True,
            sqlite_where=text("is_active IS TRUE"),
            postgresql_where=text("is_active IS TRUE"),
        ),
        Index("ix_inbox_conversation_assignments_person", "person_id", "is_active"),
        Index("ix_inbox_conversation_assignments_team", "service_team_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assigned_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation", back_populates="assignments")
    service_team = relationship("ServiceTeam")
