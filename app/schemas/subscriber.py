from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.subscriber import AccountRoleType, AccountStatus, AddressType
from app.models.subscription_engine import SettingValueType


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
    """Subscriber in the unified party model - always linked to a Person."""
    person_id: UUID  # Required - links to Person
    subscriber_number: str | None = Field(default=None, max_length=80)
    is_active: bool = True
    notes: str | None = None
    account_start_date: datetime | None = None
    # Legacy field kept for migration compatibility
    organization_id: UUID | None = None


class SubscriberCreate(SubscriberBase):
    pass


class SubscriberUpdate(BaseModel):
    person_id: UUID | None = None
    subscriber_number: str | None = Field(default=None, max_length=80)
    is_active: bool | None = None
    notes: str | None = None
    account_start_date: datetime | None = None
    # Legacy field kept for migration compatibility
    organization_id: UUID | None = None


class SubscriberRead(SubscriberBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime

    accounts: list[SubscriberAccountRead] = Field(default_factory=list)
    addresses: list[AddressRead] = Field(default_factory=list)
    custom_fields: list["SubscriberCustomFieldRead"] = Field(default_factory=list)


class SubscriberAccountBase(BaseModel):
    subscriber_id: UUID
    reseller_id: UUID | None = None
    tax_rate_id: UUID | None = None
    account_number: str | None = Field(default=None, max_length=80)
    status: AccountStatus = AccountStatus.active
    billing_enabled: bool = True
    billing_person: str | None = Field(default=None, max_length=160)
    billing_street_1: str | None = Field(default=None, max_length=160)
    billing_zip_code: str | None = Field(default=None, max_length=20)
    billing_city: str | None = Field(default=None, max_length=80)
    deposit: Decimal | None = None
    payment_method: str | None = Field(default=None, max_length=80)
    billing_date: int | None = None
    billing_due: int | None = None
    grace_period: int | None = None
    min_balance: Decimal | None = None
    month_price: Decimal | None = None
    notes: str | None = None


class SubscriberAccountCreate(SubscriberAccountBase):
    pass


class SubscriberAccountUpdate(BaseModel):
    subscriber_id: UUID | None = None
    reseller_id: UUID | None = None
    tax_rate_id: UUID | None = None
    account_number: str | None = Field(default=None, max_length=80)
    status: AccountStatus | None = None
    billing_enabled: bool | None = None
    billing_person: str | None = Field(default=None, max_length=160)
    billing_street_1: str | None = Field(default=None, max_length=160)
    billing_zip_code: str | None = Field(default=None, max_length=20)
    billing_city: str | None = Field(default=None, max_length=80)
    deposit: Decimal | None = None
    payment_method: str | None = Field(default=None, max_length=80)
    billing_date: int | None = None
    billing_due: int | None = None
    grace_period: int | None = None
    min_balance: Decimal | None = None
    month_price: Decimal | None = None
    notes: str | None = None


class SubscriberAccountRead(SubscriberAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime

    account_roles: list["AccountRoleRead"] = Field(default_factory=list)


class AddressBase(BaseModel):
    subscriber_id: UUID
    account_id: UUID | None = None
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
    account_id: UUID | None = None
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
    value_type: SettingValueType = SettingValueType.string
    value_text: str | None = None
    value_json: dict | None = None
    is_active: bool = True


class SubscriberCustomFieldCreate(SubscriberCustomFieldBase):
    pass


class SubscriberCustomFieldUpdate(BaseModel):
    subscriber_id: UUID | None = None
    key: str | None = Field(default=None, min_length=1, max_length=120)
    value_type: SettingValueType | None = None
    value_text: str | None = None
    value_json: dict | None = None
    is_active: bool | None = None


class SubscriberCustomFieldRead(SubscriberCustomFieldBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AccountRoleBase(BaseModel):
    """Links a Person to a SubscriberAccount with a specific role."""
    account_id: UUID
    person_id: UUID
    role: AccountRoleType = AccountRoleType.primary
    is_primary: bool = False
    title: str | None = Field(default=None, max_length=120)
    notes: str | None = None


class AccountRoleCreate(AccountRoleBase):
    pass


class AccountRoleUpdate(BaseModel):
    account_id: UUID | None = None
    person_id: UUID | None = None
    role: AccountRoleType | None = None
    is_primary: bool | None = None
    title: str | None = Field(default=None, max_length=120)
    notes: str | None = None


class AccountRoleRead(AccountRoleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
