from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.subscription_engine import SettingValueType


class SubscriptionEngineBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str = Field(min_length=1, max_length=60)
    description: str | None = None
    is_active: bool = True


class SubscriptionEngineCreate(SubscriptionEngineBase):
    pass


class SubscriptionEngineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = None
    is_active: bool | None = None


class SubscriptionEngineRead(SubscriptionEngineBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SubscriptionEngineSettingBase(BaseModel):
    engine_id: UUID
    key: str = Field(min_length=1, max_length=120)
    value_type: SettingValueType = SettingValueType.string
    value_text: str | None = None
    value_json: dict | None = None
    is_secret: bool = False


class SubscriptionEngineSettingCreate(SubscriptionEngineSettingBase):
    pass


class SubscriptionEngineSettingUpdate(BaseModel):
    engine_id: UUID | None = None
    key: str | None = Field(default=None, min_length=1, max_length=120)
    value_type: SettingValueType | None = None
    value_text: str | None = None
    value_json: dict | None = None
    is_secret: bool | None = None


class SubscriptionEngineSettingRead(SubscriptionEngineSettingBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
