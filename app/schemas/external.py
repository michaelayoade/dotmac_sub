from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.external import ExternalEntityType


class ExternalReferenceBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    connector_config_id: UUID
    entity_type: ExternalEntityType
    entity_id: UUID
    external_id: str = Field(min_length=1, max_length=200)
    external_url: str | None = Field(default=None, max_length=500)
    metadata_: dict | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata", "metadata_"),
        serialization_alias="metadata",
    )
    last_synced_at: datetime | None = None
    is_active: bool = True


class ExternalReferenceCreate(ExternalReferenceBase):
    pass


class ExternalReferenceUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connector_config_id: UUID | None = None
    entity_type: ExternalEntityType | None = None
    entity_id: UUID | None = None
    external_id: str | None = Field(default=None, min_length=1, max_length=200)
    external_url: str | None = Field(default=None, max_length=500)
    metadata_: dict | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata", "metadata_"),
        serialization_alias="metadata",
    )
    last_synced_at: datetime | None = None
    is_active: bool | None = None


class ExternalReferenceSync(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connector_config_id: UUID
    entity_type: ExternalEntityType
    entity_id: UUID
    external_id: str = Field(min_length=1, max_length=200)
    external_url: str | None = Field(default=None, max_length=500)
    metadata_: dict | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata", "metadata_"),
        serialization_alias="metadata",
    )
    is_active: bool | None = None


class ExternalReferenceRead(ExternalReferenceBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
