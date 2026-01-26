import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Gender(enum.Enum):
    unknown = "unknown"
    female = "female"
    male = "male"
    non_binary = "non_binary"
    other = "other"


class ContactMethod(enum.Enum):
    email = "email"
    phone = "phone"
    sms = "sms"
    push = "push"


class PersonStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    archived = "archived"


class PartyStatus(enum.Enum):
    """Lifecycle status for unified party model."""
    lead = "lead"           # Prospect, minimal info
    contact = "contact"     # Known individual, verified
    customer = "customer"   # Converted (accepted quote/signed up)
    subscriber = "subscriber"  # Active billing account


class ChannelType(enum.Enum):
    """Communication channel types for PersonChannel."""
    email = "email"
    phone = "phone"
    sms = "sms"
    whatsapp = "whatsapp"
    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"


class Person(Base):
    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    bio: Mapped[str | None] = mapped_column(Text)

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str | None] = mapped_column(String(40))

    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[Gender] = mapped_column(Enum(Gender), default=Gender.unknown)

    preferred_contact_method: Mapped[ContactMethod | None] = mapped_column(
        Enum(ContactMethod)
    )
    locale: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str | None] = mapped_column(String(64))

    address_line1: Mapped[str | None] = mapped_column(String(120))
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))

    # Unified party model fields
    party_status: Mapped[PartyStatus] = mapped_column(
        Enum(PartyStatus), default=PartyStatus.contact
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )

    status: Mapped[PersonStatus] = mapped_column(
        Enum(PersonStatus), default=PersonStatus.active
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)

    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    organization = relationship("Organization", back_populates="people")
    subscribers = relationship("Subscriber", back_populates="person")
    channels = relationship("PersonChannel", back_populates="person", cascade="all, delete-orphan")
    status_logs = relationship(
        "PersonStatusLog",
        back_populates="person",
        cascade="all, delete-orphan",
        foreign_keys="PersonStatusLog.person_id",
    )
    leads = relationship("Lead", back_populates="person")
    quotes = relationship("Quote", back_populates="person")
    sales_orders = relationship("SalesOrder", back_populates="person")
    conversations = relationship("Conversation", back_populates="person")

    @hybrid_property
    def person_id(self):
        return self.id

    @person_id.expression
    def person_id(cls):
        return cls.id


class PersonChannel(Base):
    """Communication channels for a person (email, phone, social, etc.)."""
    __tablename__ = "person_channels"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "channel_type", "address",
            name="uq_person_channels_person_type_address"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=False
    )
    channel_type: Mapped[ChannelType] = mapped_column(Enum(ChannelType), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))  # "Work", "Personal", etc.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", MutableDict.as_mutable(JSON))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    person = relationship("Person", back_populates="channels")


class PersonStatusLog(Base):
    """Audit log for person party status transitions."""
    __tablename__ = "person_status_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=False
    )
    from_status: Mapped[PartyStatus | None] = mapped_column(Enum(PartyStatus))
    to_status: Mapped[PartyStatus] = mapped_column(Enum(PartyStatus), nullable=False)
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id")
    )
    reason: Mapped[str | None] = mapped_column(String(255))
    metadata_: Mapped[dict | None] = mapped_column("metadata", MutableDict.as_mutable(JSON))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    person = relationship("Person", back_populates="status_logs", foreign_keys=[person_id])
    changed_by = relationship("Person", foreign_keys=[changed_by_id])


class PersonMergeLog(Base):
    """Audit log for person merge operations."""
    __tablename__ = "person_merge_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=False
    )
    merged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id")
    )
    source_snapshot: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON))

    merged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    target_person = relationship("Person", foreign_keys=[target_person_id])
    merged_by = relationship("Person", foreign_keys=[merged_by_id])
