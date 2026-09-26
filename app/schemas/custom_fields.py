"""Typed API contracts for custom-field values."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class CustomFieldValueWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition_id: UUID
    value: JsonValue | None = Field(
        default=None,
        description="A value matching the registered definition; null clears an optional field.",
    )


class CustomFieldValueItem(BaseModel):
    definition_id: UUID
    key: str
    label: str
    field_type: str
    section: str
    required: bool
    sensitive: bool
    redacted: bool
    value: JsonValue | None


class CustomFieldValueList(BaseModel):
    target_type: str
    target_id: UUID
    items: list[CustomFieldValueItem]


class CustomFieldValueResult(BaseModel):
    definition_id: UUID
    target_id: UUID
    cleared: bool


__all__ = [
    "CustomFieldValueItem",
    "CustomFieldValueList",
    "CustomFieldValueResult",
    "CustomFieldValueWrite",
]
