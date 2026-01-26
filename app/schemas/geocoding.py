from __future__ import annotations

from pydantic import BaseModel, Field


class GeocodePreviewRequest(BaseModel):
    address_line1: str = Field(min_length=1, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    limit: int | None = Field(default=3, ge=1, le=10)


class GeocodePreviewResult(BaseModel):
    display_name: str | None = None
    latitude: float
    longitude: float
    class_name: str | None = Field(default=None, alias="class")
    type_name: str | None = Field(default=None, alias="type")
    importance: float | None = None
