from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.person import ChannelType, PartyStatus


class PartyStatusEnum(str, Enum):
    """Party lifecycle status for API responses."""
    lead = "lead"
    contact = "contact"
    customer = "customer"
    subscriber = "subscriber"


class ChannelTypeEnum(str, Enum):
    """Channel types for API responses."""
    email = "email"
    phone = "phone"
    sms = "sms"
    whatsapp = "whatsapp"
    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"


class PersonChannelBase(BaseModel):
    """Base schema for person communication channels."""
    channel_type: ChannelTypeEnum
    address: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)
    is_primary: bool = False


class PersonChannelCreate(PersonChannelBase):
    pass


class PersonChannelUpdate(BaseModel):
    channel_type: ChannelTypeEnum | None = None
    address: str | None = Field(default=None, min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)
    is_primary: bool | None = None


class PersonChannelRead(PersonChannelBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    person_id: UUID
    is_verified: bool
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PersonBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=512)
    bio: str | None = None
    email: EmailStr
    email_verified: bool = False
    phone: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: str = "unknown"
    preferred_contact_method: str | None = None
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    # Unified party model fields
    party_status: PartyStatusEnum = PartyStatusEnum.contact
    organization_id: UUID | None = None
    status: str = "active"
    is_active: bool = True
    marketing_opt_in: bool = False
    notes: str | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class PersonCreate(PersonBase):
    """Schema for creating a new person."""
    channels: list[PersonChannelCreate] = Field(default_factory=list)


class PersonUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=512)
    bio: str | None = None
    email: EmailStr | None = None
    email_verified: bool | None = None
    phone: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: str | None = None
    preferred_contact_method: str | None = None
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    # Unified party model fields
    party_status: PartyStatusEnum | None = None
    organization_id: UUID | None = None
    status: str | None = None
    is_active: bool | None = None
    marketing_opt_in: bool | None = None
    notes: str | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class PersonRead(PersonBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    channels: list[PersonChannelRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PersonStatusTransition(BaseModel):
    """Schema for transitioning person party status."""
    new_status: PartyStatusEnum
    reason: str | None = Field(default=None, max_length=255)


class PersonMergeRequest(BaseModel):
    """Schema for merging two person records."""
    source_id: UUID
    target_id: UUID


class PersonMergeLogRead(BaseModel):
    """Schema for reading person merge audit log."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_person_id: UUID
    target_person_id: UUID
    merged_by_id: UUID | None
    source_snapshot: dict | None
    merged_at: datetime


class PersonStatusLogRead(BaseModel):
    """Schema for reading person status transition log."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    person_id: UUID
    from_status: PartyStatusEnum | None
    to_status: PartyStatusEnum
    changed_by_id: UUID | None
    reason: str | None
    created_at: datetime
