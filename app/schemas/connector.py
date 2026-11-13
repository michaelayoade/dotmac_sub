from datetime import datetime
from urllib.parse import urlparse
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

    @field_serializer("base_url")
    def _mask_credential_endpoint(self, value: str | None) -> str | None:
        if not value:
            return value
        try:
            parsed = urlparse(value)
            hostname = parsed.hostname
        except ValueError:
            return None
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        return value

    @field_serializer("headers")
    def _mask_headers(self, value: dict | None) -> dict | None:
        return (
            {key: "********" if item else item for key, item in value.items()}
            if value
            else value
        )

    @field_serializer("metadata_")
    def _mask_sensitive_metadata(self, value: dict | None) -> dict | None:
        if not value:
            return value
        hints = (
            "authorization",
            "token",
            "secret",
            "password",
            "cookie",
            "api_key",
            "apikey",
            "api-key",
            "auth",
            "credential",
        )
        return {
            key: (
                "********"
                if any(hint in str(key).lower() for hint in hints) and item
                else item
            )
            for key, item in value.items()
        }

    @field_serializer("auth_config")
    def _mask_auth_config(self, value: dict | None):
        if not value:
            return value
        return {
            key: item if key in {"username", "client_id"} or not item else "********"
            for key, item in value.items()
        }
