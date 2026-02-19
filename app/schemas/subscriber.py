from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.subscriber import (
    AddressType,
    ChannelType,
    ContactMethod,
    Gender,
    SubscriberStatus,
)


class OrganizationBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    tax_id: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    notes: str | None = None


class OrganizationCreate(OrganizationBase):
    pass


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    tax_id: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    notes: str | None = None


class OrganizationRead(OrganizationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ResellerBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)
    is_active: bool = True
    notes: str | None = None


class ResellerCreate(ResellerBase):
    pass


class ResellerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None
    notes: str | None = None


class ResellerRead(ResellerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SubscriberBase(BaseModel):
    """Unified subscriber model - combines identity, account, and billing."""
    # Identity fields
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=512)
    email: EmailStr
    email_verified: bool = False
    phone: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: Gender = Gender.unknown
    preferred_contact_method: ContactMethod | None = None
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)

    # Contact address
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)

    # Account fields
    subscriber_number: str | None = Field(default=None, max_length=80)
    account_number: str | None = Field(default=None, max_length=80)
    account_start_date: datetime | None = None
    status: SubscriberStatus = SubscriberStatus.active
    is_active: bool = True
    marketing_opt_in: bool = False

    # Organization & Reseller
    organization_id: UUID | None = None
    reseller_id: UUID | None = None

    # Billing fields
    tax_rate_id: UUID | None = None
    billing_enabled: bool = True
    billing_name: str | None = Field(default=None, max_length=160)
    billing_address_line1: str | None = Field(default=None, max_length=160)
    billing_address_line2: str | None = Field(default=None, max_length=120)
    billing_city: str | None = Field(default=None, max_length=80)
    billing_region: str | None = Field(default=None, max_length=80)
    billing_postal_code: str | None = Field(default=None, max_length=20)
    billing_country_code: str | None = Field(default=None, max_length=2)

    # Payment settings
    payment_method: str | None = Field(default=None, max_length=80)
    deposit: Decimal | None = None
    billing_day: int | None = None
    payment_due_days: int | None = None
    grace_period_days: int | None = None
    min_balance: Decimal | None = None

    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class SubscriberCreate(SubscriberBase):
    model_config = ConfigDict(extra="forbid")

    # Backwards-compat: allow "create" to target an existing person/subscriber row.
    person_id: UUID | None = Field(default=None, exclude=True)

    # If person_id is provided these can be omitted; service will update the existing row.
    first_name: str | None = Field(default=None, min_length=1, max_length=80)  # type: ignore[assignment]
    last_name: str | None = Field(default=None, min_length=1, max_length=80)  # type: ignore[assignment]
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _require_identity_when_creating_new(self) -> SubscriberCreate:
        if self.person_id:
            return self
        if not self.first_name or not self.last_name or not self.email:
            raise ValueError("first_name, last_name, and email are required when person_id is not provided.")
        return self


class SubscriberUpdate(BaseModel):
    # Identity fields
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=512)
    email: EmailStr | None = None
    email_verified: bool | None = None
    phone: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: Gender | None = None
    preferred_contact_method: ContactMethod | None = None
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)

    # Contact address
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)

    # Account fields
    subscriber_number: str | None = Field(default=None, max_length=80)
    account_number: str | None = Field(default=None, max_length=80)
    account_start_date: datetime | None = None
    status: SubscriberStatus | None = None
    is_active: bool | None = None
    marketing_opt_in: bool | None = None

    # Organization & Reseller
    organization_id: UUID | None = None
    reseller_id: UUID | None = None

    # Billing fields
    tax_rate_id: UUID | None = None
    billing_enabled: bool | None = None
    billing_name: str | None = Field(default=None, max_length=160)
    billing_address_line1: str | None = Field(default=None, max_length=160)
    billing_address_line2: str | None = Field(default=None, max_length=120)
    billing_city: str | None = Field(default=None, max_length=80)
    billing_region: str | None = Field(default=None, max_length=80)
    billing_postal_code: str | None = Field(default=None, max_length=20)
    billing_country_code: str | None = Field(default=None, max_length=2)

    # Payment settings
    payment_method: str | None = Field(default=None, max_length=80)
    deposit: Decimal | None = None
    billing_day: int | None = None
    payment_due_days: int | None = None
    grace_period_days: int | None = None
    min_balance: Decimal | None = None

    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class SubscriberRead(SubscriberBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime

    addresses: list[AddressRead] = Field(default_factory=list)
    channels: list[SubscriberChannelRead] = Field(default_factory=list)
    custom_fields: list[SubscriberCustomFieldRead] = Field(default_factory=list)


class SubscriberChannelBase(BaseModel):
    """Communication channel for a subscriber."""
    subscriber_id: UUID
    channel_type: ChannelType
    address: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)
    is_primary: bool = False
    is_verified: bool = False


class SubscriberChannelCreate(SubscriberChannelBase):
    pass


class SubscriberChannelUpdate(BaseModel):
    subscriber_id: UUID | None = None
    channel_type: ChannelType | None = None
    address: str | None = Field(default=None, min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)
    is_primary: bool | None = None
    is_verified: bool | None = None


class SubscriberChannelRead(SubscriberChannelBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AddressBase(BaseModel):
    subscriber_id: UUID
    tax_rate_id: UUID | None = None
    address_type: AddressType = AddressType.service
    label: str | None = Field(default=None, max_length=120)
    address_line1: str = Field(min_length=1, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    latitude: float | None = None
    longitude: float | None = None
    is_primary: bool = False


class AddressCreate(AddressBase):
    pass


class AddressUpdate(BaseModel):
    subscriber_id: UUID | None = None
    tax_rate_id: UUID | None = None
    address_type: AddressType | None = None
    label: str | None = Field(default=None, max_length=120)
    address_line1: str | None = Field(default=None, min_length=1, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    latitude: float | None = None
    longitude: float | None = None
    is_primary: bool | None = None


class AddressRead(AddressBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SubscriberCustomFieldBase(BaseModel):
    subscriber_id: UUID
    key: str = Field(min_length=1, max_length=120)
    value_text: str | None = None
    value_json: dict | None = None
    is_active: bool = True


class SubscriberCustomFieldCreate(SubscriberCustomFieldBase):
    pass


class SubscriberCustomFieldUpdate(BaseModel):
    subscriber_id: UUID | None = None
    key: str | None = Field(default=None, min_length=1, max_length=120)
    value_text: str | None = None
    value_json: dict | None = None
    is_active: bool | None = None


class SubscriberCustomFieldRead(SubscriberCustomFieldBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


# --- Compatibility schemas ---
# Accounts are represented by subscribers; these request/response shapes
# remain to preserve API/import compatibility.

class SubscriberAccountCreate(BaseModel):
    """Compatibility create payload for account-as-subscriber flows."""
    subscriber_id: UUID | None = None
    reseller_id: UUID | None = None
    account_number: str | None = None
    notes: str | None = None


class SubscriberAccountUpdate(BaseModel):
    """Compatibility update payload for account-as-subscriber flows."""
    pass


class SubscriberAccountRead(BaseModel):
    """Compatibility read payload for account-as-subscriber flows."""
    model_config = ConfigDict(from_attributes=True)
    id: UUID | None = None
