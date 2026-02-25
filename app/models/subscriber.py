import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db import Base
from app.models.subscription_engine import SettingValueType


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


class SubscriberStatus(enum.Enum):
    """Account/billing status for subscriber."""
    active = "active"
    suspended = "suspended"
    canceled = "canceled"
    delinquent = "delinquent"


# --- Deprecated aliases for backwards compatibility ---
AccountStatus = SubscriberStatus  # Alias for legacy code


class UserType(enum.Enum):
    """Classification for admin/system user accounts."""

    system_user = "system_user"
    customer = "customer"
    reseller = "reseller"


class SubscriberCategory(enum.Enum):
    residential = "residential"
    business = "business"
    government = "government"
    ngo = "ngo"


class AddressType(enum.Enum):
    service = "service"
    billing = "billing"
    mailing = "mailing"


class ChannelType(enum.Enum):
    """Communication channel types."""
    email = "email"
    phone = "phone"
    sms = "sms"
    whatsapp = "whatsapp"


class Organization(Base):
    """Organization for B2B subscribers."""
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    tax_id: Mapped[str | None] = mapped_column(String(80))
    domain: Mapped[str | None] = mapped_column(String(120))
    website: Mapped[str | None] = mapped_column(String(255))
    address_line1: Mapped[str | None] = mapped_column(String(120))
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscribers = relationship("Subscriber", back_populates="organization")


class Reseller(Base):
    """Reseller/partner who manages subscribers."""
    __tablename__ = "resellers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscribers = relationship("Subscriber", back_populates="reseller")


class Subscriber(Base):
    """Unified subscriber model combining identity, account, and billing.

    This is the core entity representing a customer with:
    - Identity info (name, contact details)
    - Account info (subscriber number, status)
    - Billing info (payment settings, billing address)
    """
    __tablename__ = "subscribers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # === Identity Fields (from Person) ===
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(512))

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

    # Contact address
    address_line1: Mapped[str | None] = mapped_column(String(120))
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))

    # === Account Fields (from Subscriber + SubscriberAccount) ===
    subscriber_number: Mapped[str | None] = mapped_column(String(80), unique=True)
    account_number: Mapped[str | None] = mapped_column(String(80))
    account_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[SubscriberStatus] = mapped_column(
        Enum(SubscriberStatus), default=SubscriberStatus.active
    )
    user_type: Mapped[UserType] = mapped_column(
        Enum(UserType), default=UserType.system_user
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)

    # === Organization & Reseller ===
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )

    # === Billing Fields (from SubscriberAccount) ===
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    billing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Billing address (can differ from contact address)
    billing_name: Mapped[str | None] = mapped_column(String(160))
    billing_address_line1: Mapped[str | None] = mapped_column(String(160))
    billing_address_line2: Mapped[str | None] = mapped_column(String(120))
    billing_city: Mapped[str | None] = mapped_column(String(80))
    billing_region: Mapped[str | None] = mapped_column(String(80))
    billing_postal_code: Mapped[str | None] = mapped_column(String(20))
    billing_country_code: Mapped[str | None] = mapped_column(String(2))

    # Payment settings
    payment_method: Mapped[str | None] = mapped_column(String(80))
    deposit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    billing_day: Mapped[int | None] = mapped_column(Integer)  # Day of month for billing
    payment_due_days: Mapped[int | None] = mapped_column(Integer)  # Days after invoice
    grace_period_days: Mapped[int | None] = mapped_column(Integer)

    # Prepaid/balance settings
    min_balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    prepaid_low_balance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prepaid_deactivation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # === Common Fields ===
    notes: Mapped[str | None] = mapped_column(Text)
    splynx_customer_id: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    # === Relationships ===
    organization = relationship("Organization", back_populates="subscribers")
    reseller = relationship("Reseller", back_populates="subscribers")
    tax_rate = relationship("TaxRate")

    addresses = relationship("Address", back_populates="subscriber", cascade="all, delete-orphan")
    custom_fields = relationship("SubscriberCustomField", back_populates="subscriber", cascade="all, delete-orphan")
    channels = relationship("SubscriberChannel", back_populates="subscriber", cascade="all, delete-orphan")

    # Service relationships
    subscriptions = relationship("Subscription", back_populates="subscriber")
    access_credentials = relationship("AccessCredential", back_populates="subscriber")
    service_orders = relationship("ServiceOrder", back_populates="subscriber")
    cpe_devices = relationship("CPEDevice", back_populates="subscriber")
    ip_assignments = relationship("IPAssignment", back_populates="subscriber")
    ont_assignments = relationship("OntAssignment", back_populates="subscriber")
    dunning_cases = relationship("DunningCase", back_populates="subscriber")

    @property
    def full_name(self) -> str:
        """Return full name."""
        return f"{self.first_name} {self.last_name}"

    @property
    def category(self) -> SubscriberCategory:
        """Normalized subscriber category stored in metadata."""
        raw = (self.metadata_ or {}).get("subscriber_category")
        if isinstance(raw, str):
            try:
                return SubscriberCategory(raw)
            except ValueError:
                pass
        return SubscriberCategory.residential

    @category.setter
    def category(self, value: SubscriberCategory | str | None) -> None:
        if isinstance(value, SubscriberCategory):
            normalized = value.value
        elif isinstance(value, str):
            normalized = value.strip().lower()
            try:
                normalized = SubscriberCategory(normalized).value
            except ValueError:
                normalized = SubscriberCategory.residential.value
        else:
            normalized = SubscriberCategory.residential.value

        metadata = dict(self.metadata_ or {})
        metadata["subscriber_category"] = normalized
        self.metadata_ = metadata


class SubscriberChannel(Base):
    """Additional communication channels for a subscriber."""
    __tablename__ = "subscriber_channels"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id", "channel_type", "address",
            name="uq_subscriber_channels_subscriber_type_address"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    channel_type: Mapped[ChannelType] = mapped_column(Enum(ChannelType), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", MutableDict.as_mutable(JSON()))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="channels")


class SubscriberCustomField(Base):
    """Custom fields for subscribers."""
    __tablename__ = "subscriber_custom_fields"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id", "key", name="uq_subscriber_custom_fields_subscriber_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value_type: Mapped[SettingValueType] = mapped_column(
        Enum(SettingValueType), default=SettingValueType.string
    )
    value_text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="custom_fields")


class Address(Base):
    """Service/installation addresses for subscribers."""
    __tablename__ = "addresses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    address_type: Mapped[AddressType] = mapped_column(
        Enum(AddressType), default=AddressType.service
    )
    label: Mapped[str | None] = mapped_column(String(120))
    address_line1: Mapped[str] = mapped_column(String(120), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="addresses")
    tax_rate = relationship("TaxRate")


# --- Compatibility aliases ---
# SubscriberAccount is now represented by Subscriber.
SubscriberAccount = Subscriber


class ResellerUser(Base):
    """Reseller user linkage model.

    Maps to the active reseller_users table while preserving
    subscriber_id/person_id compatibility across callers.
    """
    __tablename__ = "reseller_users"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # DB column name is person_id in legacy schemas.
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column("person_id", UUID(as_uuid=True))
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID | None] = synonym("subscriber_id")
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
