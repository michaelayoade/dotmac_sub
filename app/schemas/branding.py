from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BrandProfileUpdate(BaseModel):
    scope_id: uuid.UUID | None = None
    brand_name: str | None = Field(default=None, max_length=120)
    product_name: str | None = Field(default=None, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    tagline: str | None = Field(default=None, max_length=255)
    primary_color: str | None = None
    secondary_color: str | None = None
    logo_url: str | None = None
    dark_logo_url: str | None = None
    favicon_url: str | None = None
    support_email: str | None = Field(default=None, max_length=255)
    support_phone: str | None = Field(default=None, max_length=40)
    from_email: str | None = Field(default=None, max_length=255)
    from_name: str | None = Field(default=None, max_length=160)
    app_url: str | None = Field(default=None, max_length=512)
    portal_domain: str | None = Field(default=None, max_length=255)
    legal_address: dict[str, str] | None = None
    metadata_: dict[str, object] | None = None


class BrandProfileRead(BrandProfileUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ResolvedBrandRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    product_name: str
    legal_name: str
    tagline: str
    primary_color: str
    secondary_color: str
    semantic_colors: dict[str, str]
    logo_url: str
    dark_logo_url: str
    favicon_url: str
    support_email: str
    support_phone: str
    from_email: str
    from_name: str
    app_url: str
    portal_domain: str
    legal_address: dict[str, str]
    source_scope: str
    source_scope_id: str | None
