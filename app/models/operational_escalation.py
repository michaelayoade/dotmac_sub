import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
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


class OperationalEntityType:
    outage = "outage"
    ticket = "ticket"
    work_order = "work_order"
    project = "project"
    inbox_conversation = "inbox_conversation"
    subscriber = "subscriber"
    network_device = "network_device"
    site = "site"
    payment_incident = "payment_incident"
    provisioning_failure = "provisioning_failure"


class OperationalParticipantType:
    person = "person"
    team = "team"
    duty_role = "duty_role"
    subscriber = "subscriber"
    reseller = "reseller"
    external = "external"


class OperationalOwnerRole:
    primary = "primary"
    backup = "backup"


class OperationalWatcherRole:
    watcher = "watcher"
    lead = "lead"
    manager = "manager"
    account_manager = "account_manager"


class OperationalRoomProvider:
    nextcloud_talk = "nextcloud_talk"


class OperationalNotificationChannel:
    web = "web"
    email = "email"
    push = "push"
    nextcloud_talk = "nextcloud_talk"
    whatsapp = "whatsapp"
    sms = "sms"
    webhook = "webhook"


class OperationalEscalationStatus:
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    canceled = "canceled"


class OperationalDeliveryStatus:
    pending = "pending"
    sent = "sent"
    failed = "failed"
    acknowledged = "acknowledged"
    suppressed = "suppressed"


class OperationalOwner(Base):
    __tablename__ = "operational_owners"
    __table_args__ = (
        Index("ix_operational_owners_entity", "entity_type", "entity_id", "is_active"),
        Index("ix_operational_owners_team", "service_team_id", "is_active"),
        Index("ix_operational_owners_person", "person_id", "is_active"),
        Index(
            "uq_operational_owners_primary_active",
            "entity_type",
            "entity_id",
            unique=True,
            sqlite_where=text("is_active IS TRUE AND role = 'primary'"),
            postgresql_where=text("is_active IS TRUE AND role = 'primary'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(40), nullable=False)
    role: Mapped[str] = mapped_column(
        String(40), default=OperationalOwnerRole.primary, nullable=False
    )
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    duty_role: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class OperationalWatcher(Base):
    __tablename__ = "operational_watchers"
    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "watcher_type",
            "service_team_id",
            "person_id",
            "subscriber_id",
            "duty_role",
            name="uq_operational_watchers_target",
        ),
        Index(
            "ix_operational_watchers_entity", "entity_type", "entity_id", "is_active"
        ),
        Index("ix_operational_watchers_team", "service_team_id", "is_active"),
        Index("ix_operational_watchers_person", "person_id", "is_active"),
        Index("ix_operational_watchers_subscriber", "subscriber_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    watcher_type: Mapped[str] = mapped_column(String(40), nullable=False)
    role: Mapped[str] = mapped_column(
        String(40), default=OperationalWatcherRole.watcher, nullable=False
    )
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    duty_role: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text)
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
    subscriber = relationship("Subscriber")


class OperationalRoomLink(Base):
    __tablename__ = "operational_room_links"
    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "provider",
            "room_id",
            name="uq_operational_room_links_room",
        ),
        Index(
            "ix_operational_room_links_entity",
            "entity_type",
            "entity_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    room_id: Mapped[str] = mapped_column(String(160), nullable=False)
    room_name: Mapped[str | None] = mapped_column(String(200))
    room_url: Mapped[str | None] = mapped_column(String(500))
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


class OperationalEscalationPolicy(Base):
    __tablename__ = "operational_escalation_policies"
    __table_args__ = (
        Index(
            "ix_operational_escalation_policies_scope",
            "entity_type",
            "scope_type",
            "scope_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    scope_type: Mapped[str | None] = mapped_column(String(80))
    scope_id: Mapped[str | None] = mapped_column(String(100))
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    min_severity: Mapped[str | None] = mapped_column(String(40))
    min_affected_customers: Mapped[int | None] = mapped_column(Integer)
    vip_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unowned_after_seconds: Mapped[int | None] = mapped_column(Integer)
    stale_owner_update_seconds: Mapped[int | None] = mapped_column(Integer)
    customer_update_due_within_seconds: Mapped[int | None] = mapped_column(Integer)
    unresolved_after_seconds: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[list | None] = mapped_column(JSON)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
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


class OperationalEscalationEvent(Base):
    __tablename__ = "operational_escalation_events"
    __table_args__ = (
        Index(
            "ix_operational_escalation_events_entity",
            "entity_type",
            "entity_id",
            "status",
        ),
        Index(
            "ix_operational_escalation_events_policy",
            "policy_id",
            "triggered_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operational_escalation_policies.id")
    )
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trigger: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(40))
    affected_customer_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(40), default=OperationalEscalationStatus.open, nullable=False
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    acknowledged_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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

    policy = relationship("OperationalEscalationPolicy")
    deliveries = relationship(
        "OperationalEscalationDelivery",
        back_populates="event",
        cascade="all, delete-orphan",
    )


class OperationalEscalationDelivery(Base):
    __tablename__ = "operational_escalation_deliveries"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_operational_escalation_delivery_dedup"),
        Index(
            "ix_operational_escalation_deliveries_event",
            "event_id",
            "delivery_status",
        ),
        Index(
            "ix_operational_escalation_deliveries_recipient",
            "recipient_type",
            "recipient_id",
            "channel",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("operational_escalation_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    watcher_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operational_watchers.id")
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operational_owners.id")
    )
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient_type: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient_id: Mapped[str | None] = mapped_column(String(100))
    recipient_address: Mapped[str | None] = mapped_column(String(255))
    delivery_status: Mapped[str] = mapped_column(
        String(40), default=OperationalDeliveryStatus.pending, nullable=False
    )
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    escalation_level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
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

    event = relationship("OperationalEscalationEvent", back_populates="deliveries")
    watcher = relationship("OperationalWatcher")
    owner = relationship("OperationalOwner")
