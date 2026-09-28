"""Typed contracts for Network Map KMZ import and export."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.models.audit import AuditActorType
from app.models.network import FiberSegmentType
from app.models.network_monitoring import DeviceType
from app.models.subscriber import SubscriberStatus
from app.services.device_operational_status import DeviceOperationalState
from app.services.network_map_contracts import (
    NetworkMapInspectionStatus,
    NetworkMapSignalQuality,
    NetworkMapSupportLifecycle,
)
from app.services.owner_commands import CommandContext


class NetworkMapImportProfile(StrEnum):
    osp_paths = "osp_paths"
    osp_access_points = "osp_access_points"
    osp_cabinets = "osp_cabinets"
    osp_splice_info = "osp_splice_info"
    osp_buildings = "osp_buildings"
    osp_air_fiber = "osp_air_fiber"

    @property
    def label(self) -> str:
        return {
            NetworkMapImportProfile.osp_paths: "Fiber paths",
            NetworkMapImportProfile.osp_access_points: "Access points",
            NetworkMapImportProfile.osp_cabinets: "FDH cabinets",
            NetworkMapImportProfile.osp_splice_info: "Splice closures",
            NetworkMapImportProfile.osp_buildings: "Service buildings",
            NetworkMapImportProfile.osp_air_fiber: "Support structures",
        }[self]


class NetworkMapImportStatus(StrEnum):
    staged = "staged"
    blocked = "blocked"


class NetworkMapImportMatchStatus(StrEnum):
    new = "new"
    unchanged = "unchanged"
    exact_external = "exact_external"
    candidate = "candidate"
    ambiguous = "ambiguous"
    blocked = "blocked"


class NetworkMapGeometryType(StrEnum):
    point = "Point"
    line_string = "LineString"
    polygon = "Polygon"
    geometry_collection = "GeometryCollection"


@dataclass(frozen=True, slots=True)
class NetworkMapCoordinate:
    longitude: float
    latitude: float

    def to_transport(self) -> list[float]:
        return [self.longitude, self.latitude]


@dataclass(frozen=True, slots=True)
class NetworkMapImportedGeometry:
    geometry_type: NetworkMapGeometryType
    coordinates: tuple[NetworkMapCoordinate, ...]

    def to_transport(self) -> dict[str, object]:
        coordinates: object
        if self.geometry_type is NetworkMapGeometryType.geometry_collection:
            return {"type": self.geometry_type.value, "geometries": []}
        if self.geometry_type is NetworkMapGeometryType.point:
            coordinates = self.coordinates[0].to_transport()
        elif self.geometry_type is NetworkMapGeometryType.polygon:
            coordinates = [
                [coordinate.to_transport() for coordinate in self.coordinates]
            ]
        else:
            coordinates = [coordinate.to_transport() for coordinate in self.coordinates]
        return {"type": self.geometry_type.value, "coordinates": coordinates}


@dataclass(frozen=True, slots=True)
class NetworkMapImportedFeature:
    row_number: int
    asset_type: str
    external_id: str | None
    display_name: str | None
    geometry: NetworkMapImportedGeometry
    match_status: NetworkMapImportMatchStatus
    blocker_codes: tuple[str, ...]
    match_reasons: tuple[str, ...]
    candidate_asset_ids: tuple[str, ...]

    def to_transport(self) -> dict[str, object]:
        return {
            "type": "Feature",
            "geometry": self.geometry.to_transport(),
            "properties": {
                "row_number": self.row_number,
                "asset_type": self.asset_type,
                "external_id": self.external_id,
                "name": self.display_name or self.external_id or "Imported feature",
                "match_status": self.match_status.value,
                "blocker_codes": list(self.blocker_codes),
                "match_reasons": list(self.match_reasons),
                "candidate_asset_ids": list(self.candidate_asset_ids),
                "preview": True,
            },
        }


@dataclass(frozen=True, slots=True)
class StageNetworkMapKmzCommand:
    context: CommandContext
    actor_id: UUID
    actor_type: AuditActorType
    actor_label: str
    filename: str
    content: bytes
    profile: NetworkMapImportProfile


@dataclass(frozen=True, slots=True)
class NetworkMapKmzImportOutcome:
    batch_id: UUID
    created: bool
    status: NetworkMapImportStatus
    profile: NetworkMapImportProfile
    source_name: str
    file_sha256: str
    manifest_sha256: str
    feature_count: int
    blocker_count: int
    candidate_count: int
    new_count: int
    unchanged_count: int
    features: tuple[NetworkMapImportedFeature, ...]
    preview_truncated: bool

    def to_transport(self) -> dict[str, object]:
        return {
            "batch_id": str(self.batch_id),
            "created": self.created,
            "status": self.status.value,
            "profile": self.profile.value,
            "source_name": self.source_name,
            "file_sha256": self.file_sha256,
            "manifest_sha256": self.manifest_sha256,
            "feature_count": self.feature_count,
            "blocker_count": self.blocker_count,
            "candidate_count": self.candidate_count,
            "new_count": self.new_count,
            "unchanged_count": self.unchanged_count,
            "features": [feature.to_transport() for feature in self.features],
            "preview_truncated": self.preview_truncated,
        }


class NetworkMapExportLayer(StrEnum):
    infrastructure = "infrastructure"
    fiber = "fiber"
    network_devices = "network_devices"
    onts = "onts"
    customers = "customers"


class NetworkMapExportScope(StrEnum):
    visible = "visible"
    all = "all"


@dataclass(frozen=True, slots=True)
class NetworkMapBounds:
    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True, slots=True)
class NetworkMapKmzExportQuery:
    layers: tuple[NetworkMapExportLayer, ...]
    scope: NetworkMapExportScope
    bounds: NetworkMapBounds | None
    include_customers: bool
    customer_status: SubscriberStatus | None = None
    device_status: DeviceOperationalState | None = None
    device_type: DeviceType | None = None
    ont_status: DeviceOperationalState | None = None
    signal_quality: NetworkMapSignalQuality | None = None
    support_lifecycle: NetworkMapSupportLifecycle | None = None
    inspection_status: NetworkMapInspectionStatus | None = None
    segment_type: FiberSegmentType | None = None


@dataclass(frozen=True, slots=True)
class NetworkMapKmzExportOutcome:
    filename: str
    content: bytes
    feature_count: int
    file_sha256: str


__all__ = [
    "NetworkMapBounds",
    "NetworkMapCoordinate",
    "NetworkMapExportLayer",
    "NetworkMapExportScope",
    "NetworkMapGeometryType",
    "NetworkMapImportedFeature",
    "NetworkMapImportedGeometry",
    "NetworkMapImportMatchStatus",
    "NetworkMapImportProfile",
    "NetworkMapImportStatus",
    "NetworkMapKmzExportOutcome",
    "NetworkMapKmzExportQuery",
    "NetworkMapKmzImportOutcome",
    "StageNetworkMapKmzCommand",
]
