import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.subscription_engine import SettingValueType


class AddressType(enum.Enum):
    service = "service"
    billing = "billing"
    mailing = "mailing"


class AccountRoleType(enum.Enum):
    """Role types for AccountRole (person-to-account relationships)."""
    primary = "primary"      # Main account holder
    billing = "billing"      # Receives invoices
    technical = "technical"  # Technical contact
    support = "support"      # Support requests


class AccountStatus(enum.Enum):
    active = "active"
    suspended = "suspended"
    canceled = "canceled"
    delinquent = "delinquent"


class Organization(Base):
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    people = relationship("Person", back_populates="organization")


class Reseller(Base):
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    accounts = relationship("SubscriberAccount", back_populates="reseller")
    users = relationship("ResellerUser", back_populates="reseller")


class ResellerUser(Base):
    __tablename__ = "reseller_users"
    __table_args__ = (
        UniqueConstraint("reseller_id", "person_id", name="uq_reseller_users_reseller_person"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    reseller = relationship("Reseller", back_populates="users")
    person = relationship("Person")


class Subscriber(Base):
    """Subscriber represents a billing entity linked to a Person.

    In the unified party model, subscribers are always person-based.
    For B2B cases, the Person has an organization_id set.
    Organization info is accessible via subscriber.person.organization.
    """
    __tablename__ = "subscribers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_number: Mapped[str | None] = mapped_column(String(80), unique=True)
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    account_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    person = relationship("Person", back_populates="subscribers")
    accounts = relationship("SubscriberAccount", back_populates="subscriber")
    addresses = relationship("Address", back_populates="subscriber")
    custom_fields = relationship("SubscriberCustomField", back_populates="subscriber")

    @hybrid_property
    def organization_id(self):
        return self.person.organization_id if self.person else None

    @organization_id.expression
    def organization_id(cls):
        from app.models.person import Person

        return (
            select(Person.organization_id)
            .where(Person.id == cls.person_id)
            .scalar_subquery()
        )

    @property
    def organization(self):
        return self.person.organization if self.person else None


class SubscriberCustomField(Base):
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    subscriber = relationship("Subscriber", back_populates="custom_fields")


class SubscriberAccount(Base):
    __tablename__ = "subscriber_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    account_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), default=AccountStatus.active
    )
    billing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    billing_person: Mapped[str | None] = mapped_column(String(160))
    billing_street_1: Mapped[str | None] = mapped_column(String(160))
    billing_zip_code: Mapped[str | None] = mapped_column(String(20))
    billing_city: Mapped[str | None] = mapped_column(String(80))
    deposit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[str | None] = mapped_column(String(80))
    billing_date: Mapped[int | None] = mapped_column(Integer)
    billing_due: Mapped[int | None] = mapped_column(Integer)
    grace_period: Mapped[int | None] = mapped_column(Integer)
    min_balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    month_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    prepaid_low_balance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prepaid_deactivation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    subscriber = relationship("Subscriber", back_populates="accounts")
    reseller = relationship("Reseller", back_populates="accounts")
    tax_rate = relationship("TaxRate")
    account_roles = relationship("AccountRole", back_populates="account", cascade="all, delete-orphan")
    addresses = relationship("Address", back_populates="account")
    subscriptions = relationship("Subscription", back_populates="account")
    access_credentials = relationship("AccessCredential", back_populates="account")
    service_orders = relationship("ServiceOrder", back_populates="account")
    cpe_devices = relationship("CPEDevice", back_populates="account")
    ip_assignments = relationship("IPAssignment", back_populates="account")
    ont_assignments = relationship("OntAssignment", back_populates="account")
    dunning_cases = relationship("DunningCase", back_populates="account")


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriber_accounts.id")
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    subscriber = relationship("Subscriber", back_populates="addresses")
    account = relationship("SubscriberAccount", back_populates="addresses")
    tax_rate = relationship("TaxRate")


class AccountRole(Base):
    """Links a Person to a SubscriberAccount with a specific role.

    This replaces the legacy Contact model, providing a unified way to
    associate people with accounts in various capacities (billing, technical, etc.).
    """
    __tablename__ = "account_roles"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "person_id", "role",
            name="uq_account_roles_account_person_role"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriber_accounts.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id"), nullable=False
    )
    role: Mapped[AccountRoleType] = mapped_column(
        Enum(AccountRoleType), default=AccountRoleType.primary
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str | None] = mapped_column(String(120))  # Job title
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    account = relationship("SubscriberAccount", back_populates="account_roles")
    person = relationship("Person")
