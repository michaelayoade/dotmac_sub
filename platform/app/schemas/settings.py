from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType


class DomainSettingBase(BaseModel):
    domain: SettingDomain
    key: str
    value_type: SettingValueType = SettingValueType.string
    value_text: str | None = None
    value_json: dict | list | bool | int | str | None = None
    is_secret: bool = False
    is_active: bool = True


class DomainSettingCreate(DomainSettingBase):
    @model_validator(mode="after")
    def _validate_value_alignment(self) -> "DomainSettingCreate":
        if self.value_type == SettingValueType.json:
            if self.value_json is None or self.value_text is not None:
                raise ValueError("json settings require value_json only.")
        else:
            if self.value_text is None or self.value_json is not None:
                raise ValueError("non-json settings require value_text only.")
        return self


class DomainSettingUpdate(BaseModel):
    domain: SettingDomain | None = None
    key: str | None = None
    value_type: SettingValueType | None = None
    value_text: str | None = None
    value_json: dict | list | bool | int | str | None = None
    is_secret: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_value_alignment(self) -> "DomainSettingUpdate":
        fields_set = self.model_fields_set
        if {"value_type", "value_text", "value_json"} & fields_set:
            if self.value_type == SettingValueType.json:
                if self.value_json is None or self.value_text is not None:
                    raise ValueError("json settings require value_json only.")
            elif self.value_type is not None:
                if self.value_text is None or self.value_json is not None:
                    raise ValueError("non-json settings require value_text only.")
            else:
                if self.value_text is not None and self.value_json is not None:
                    raise ValueError("Provide only one of value_text or value_json.")
                if self.value_json is not None:
                    raise ValueError("value_type is required when setting value_json.")
        return self


class DomainSettingRead(DomainSettingBase):
    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
