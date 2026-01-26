import enum
import uuid
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class GeoLocationType(enum.Enum):
    address = "address"
    pop = "pop"
    site = "site"
    customer = "customer"
    asset = "asset"
    custom = "custom"


class GeoAreaType(enum.Enum):
    coverage = "coverage"
    service_area = "service_area"
    region = "region"
    custom = "custom"


class GeoLayerType(enum.Enum):
    points = "points"
    lines = "lines"
    polygons = "polygons"
    heatmap = "heatmap"
    cluster = "cluster"


class GeoLayerSource(enum.Enum):
    locations = "locations"
    areas = "areas"


class GeoLocation(Base):
    __tablename__ = "geo_locations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    location_type: Mapped[GeoLocationType] = mapped_column(
        Enum(GeoLocationType), default=GeoLocationType.custom
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    address = relationship("Address")
    pop_site = relationship("PopSite")


class ServiceBuilding(Base):
    """Building/premises for service delivery with CLLI codes."""

    __tablename__ = "service_buildings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    clli: Mapped[str | None] = mapped_column(String(20))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    boundary_geom = mapped_column(Geometry("POLYGON", srid=4326), nullable=True)
    street: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(60))
    zip_code: Mapped[str | None] = mapped_column(String(20))
    work_order: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class GeoArea(Base):
    __tablename__ = "geo_areas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    area_type: Mapped[GeoAreaType] = mapped_column(
        Enum(GeoAreaType), default=GeoAreaType.custom
    )
    geometry_geojson: Mapped[dict | None] = mapped_column(JSON)
    geom = mapped_column(Geometry("GEOMETRY", srid=4326), nullable=True)
    min_latitude: Mapped[float | None] = mapped_column(Float)
    min_longitude: Mapped[float | None] = mapped_column(Float)
    max_latitude: Mapped[float | None] = mapped_column(Float)
    max_longitude: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class GeoLayer(Base):
    __tablename__ = "geo_layers"
    __table_args__ = (UniqueConstraint("layer_key", name="uq_geo_layers_layer_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    layer_key: Mapped[str] = mapped_column(String(80), nullable=False)
    layer_type: Mapped[GeoLayerType] = mapped_column(
        Enum(GeoLayerType), default=GeoLayerType.points
    )
    source_type: Mapped[GeoLayerSource] = mapped_column(
        Enum(GeoLayerSource), default=GeoLayerSource.locations
    )
    style: Mapped[dict | None] = mapped_column(JSON)
    filters: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
