from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.gis import GeoAreaType, GeoLayerSource, GeoLayerType, GeoLocationType


class GeoLocationBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=160)
    location_type: GeoLocationType = GeoLocationType.custom
    latitude: float
    longitude: float
    address_id: UUID | None = None
    pop_site_id: UUID | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    tags: list[str] | None = None
    notes: str | None = None
    is_active: bool = True


class GeoLocationCreate(GeoLocationBase):
    pass


class GeoLocationUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    location_type: GeoLocationType | None = None
    latitude: float | None = None
    longitude: float | None = None
    address_id: UUID | None = None
    pop_site_id: UUID | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    tags: list[str] | None = None
    notes: str | None = None
    is_active: bool | None = None


class GeoLocationRead(GeoLocationBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class GeoAreaBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=160)
    area_type: GeoAreaType = GeoAreaType.custom
    geometry_geojson: dict | None = None
    min_latitude: float | None = None
    min_longitude: float | None = None
    max_latitude: float | None = None
    max_longitude: float | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    tags: list[str] | None = None
    notes: str | None = None
    is_active: bool = True


class GeoAreaCreate(GeoAreaBase):
    pass


class GeoAreaUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    area_type: GeoAreaType | None = None
    geometry_geojson: dict | None = None
    min_latitude: float | None = None
    min_longitude: float | None = None
    max_latitude: float | None = None
    max_longitude: float | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    tags: list[str] | None = None
    notes: str | None = None
    is_active: bool | None = None


class GeoAreaRead(GeoAreaBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class GeoLayerBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    layer_key: str = Field(min_length=1, max_length=80)
    layer_type: GeoLayerType = GeoLayerType.points
    source_type: GeoLayerSource = GeoLayerSource.locations
    style: dict | None = None
    filters: dict | None = None
    is_active: bool = True


class GeoLayerCreate(GeoLayerBase):
    pass


class GeoLayerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    layer_key: str | None = Field(default=None, min_length=1, max_length=80)
    layer_type: GeoLayerType | None = None
    source_type: GeoLayerSource | None = None
    style: dict | None = None
    filters: dict | None = None
    is_active: bool | None = None


class GeoLayerRead(GeoLayerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class GeoFeatureRead(BaseModel):
    type: str = "Feature"
    id: str
    geometry: dict | None = None
    properties: dict | None = None


class GeoFeatureCollectionRead(BaseModel):
    type: str = "FeatureCollection"
    features: list[GeoFeatureRead]


class ElevationRead(BaseModel):
    latitude: float
    longitude: float
    elevation_m: int | None = None
    tile: str
    source: str
    available: bool
    void: bool


class MyLocationRequestCreate(BaseModel):
    """Customer self-care pin correction. Coordinates only — the `/me`
    endpoint forces subscriber_id to the caller."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    note: str | None = Field(default=None, max_length=2000)


class MyLocationRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    requested_latitude: float
    requested_longitude: float
    customer_note: str | None = None
    review_note: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _status_value(cls, value):
        return getattr(value, "value", value)


class MyLocationRead(BaseModel):
    address_label: str | None = None
    current_latitude: float | None = None
    current_longitude: float | None = None
    can_submit_request: bool
    has_address_anchor: bool
    pending_request: MyLocationRequestRead | None = None
    history: list[MyLocationRequestRead] = []
