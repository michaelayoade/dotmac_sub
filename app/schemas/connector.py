from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models.connector import ConnectorAuthType, ConnectorType


class ConnectorConfigBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=160)
    connector_type: ConnectorType = ConnectorType.custom
    base_url: str | None = Field(default=None, max_length=500)
    auth_type: ConnectorAuthType = ConnectorAuthType.none
    auth_config: dict | None = None
    headers: dict | None = None
    retry_policy: dict | None = None
    timeout_sec: int | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    notes: str | None = None
    is_active: bool = True


class ConnectorConfigCreate(ConnectorConfigBase):
    pass


class ConnectorConfigUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    connector_type: ConnectorType | None = None
    base_url: str | None = Field(default=None, max_length=500)
    auth_type: ConnectorAuthType | None = None
    auth_config: dict | None = None
    headers: dict | None = None
    retry_policy: dict | None = None
    timeout_sec: int | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    notes: str | None = None
    is_active: bool | None = None


class ConnectorConfigRead(ConnectorConfigBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime

    @field_serializer("auth_config")
    def _mask_auth_config(self, value: dict | None):
        if not value:
            return value
        masked = dict(value)
        for key in ("secret", "token", "api_key", "password", "client_secret"):
            if key in masked and masked[key]:
                raw = str(masked[key])
                suffix = raw[-4:]
                masked[key] = f"{'*' * max(len(raw) - 4, 4)}{suffix}"
        return masked
