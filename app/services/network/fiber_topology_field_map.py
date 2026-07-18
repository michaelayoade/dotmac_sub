"""Exact read-only map projection for staged fiber field verification.

The projection consumes the exhaustive field-verification worklist and attaches
the exact staged GeoJSON for presentation. It never repairs geometry, snaps
features, infers topology, creates jobs or observations, or decides cutover
eligibility.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from collections import Counter
from dataclasses import dataclass
from numbers import Real

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_topology_staging import FiberTopologyStagedFeature
from app.services.network.fiber_topology_field_worklist import (
    FiberTopologyFieldWorklistReport,
    reconcile_fiber_field_worklist,
)

GEOMETRY_PRESENTATION_STATES = (
    "exact_geojson",
    "source_geometry_unrenderable",
)


class FiberTopologyFieldMapError(ValueError):
    """Raised when an exact field-map snapshot cannot be projected."""


@dataclass(frozen=True)
class FiberTopologyFieldMapReport:
    overlay_sha256: str
    worklist_report_sha256: str
    staged_feature_count: int
    source_batch_count: int
    needs_follow_up_count: int
    current_agreement_count: int
    rows_with_current_work_orders: int
    rows_with_superseded_work_orders: int
    geometry_presentation_counts: dict[str, int]
    state_counts: dict[str, int]
    priority_counts: dict[str, int]
    asset_type_counts: dict[str, int]
    source_system_counts: dict[str, int]
    source_profile_counts: dict[str, int]
    bbox: list[float] | None
    feature_collection: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_type_counts": self.asset_type_counts,
            "bbox": self.bbox,
            "current_agreement_count": self.current_agreement_count,
            "feature_collection": self.feature_collection,
            "geometry_presentation_counts": self.geometry_presentation_counts,
            "needs_follow_up_count": self.needs_follow_up_count,
            "overlay_sha256": self.overlay_sha256,
            "priority_counts": self.priority_counts,
            "rows_with_current_work_orders": self.rows_with_current_work_orders,
            "rows_with_superseded_work_orders": (self.rows_with_superseded_work_orders),
            "schema_version": 1,
            "source_batch_count": self.source_batch_count,
            "source_profile_counts": self.source_profile_counts,
            "source_system_counts": self.source_system_counts,
            "staged_feature_count": self.staged_feature_count,
            "state_counts": self.state_counts,
            "worklist_report_sha256": self.worklist_report_sha256,
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_coordinate(value: object) -> bool:
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _position(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    longitude, latitude = value[0], value[1]
    if not _is_coordinate(longitude) or not _is_coordinate(latitude):
        return None
    lon = float(longitude)
    lat = float(latitude)
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def _presentation_coordinates(
    geometry_type: str, coordinates: object
) -> list[tuple[float, float]] | None:
    """Return exact-coordinate bounds inputs only for renderable source geometry."""

    if geometry_type == "Point":
        point = _position(coordinates)
        return [point] if point is not None else None
    if geometry_type == "LineString":
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return None
        line_points = [_position(value) for value in coordinates]
        if any(point is None for point in line_points):
            return None
        return [point for point in line_points if point is not None]
    if geometry_type == "Polygon":
        if not isinstance(coordinates, list) or not coordinates:
            return None
        polygon_points: list[tuple[float, float]] = []
        for ring in coordinates:
            if not isinstance(ring, list) or len(ring) < 4:
                return None
            ring_points = [_position(value) for value in ring]
            if not all(ring_points):
                return None
            polygon_points.extend(point for point in ring_points if point is not None)
        return polygon_points
    return None


def _exact_geometry(
    feature: FiberTopologyStagedFeature,
) -> tuple[dict[str, object], str, list[tuple[float, float]]]:
    geometry = copy.deepcopy(feature.geometry_geojson)
    if not isinstance(geometry, dict):
        raise FiberTopologyFieldMapError(
            f"staged feature {feature.id} has non-object GeoJSON"
        )
    geojson_type = geometry.get("type")
    expected_type = (
        "GeometryCollection"
        if feature.geometry_type == "Unknown"
        else feature.geometry_type
    )
    if geojson_type != expected_type:
        raise FiberTopologyFieldMapError(
            f"staged feature {feature.id} geometry type does not match its source fact"
        )
    points = _presentation_coordinates(
        feature.geometry_type,
        geometry.get("coordinates"),
    )
    if points is None:
        return geometry, "source_geometry_unrenderable", []
    return geometry, "exact_geojson", points


def _features_by_id(
    db: Session, staged_feature_ids: list[str]
) -> dict[str, FiberTopologyStagedFeature]:
    if not staged_feature_ids:
        return {}
    try:
        feature_ids = [uuid.UUID(value) for value in staged_feature_ids]
    except ValueError as exc:
        raise FiberTopologyFieldMapError(
            "field-verification worklist contains an invalid staged feature identity"
        ) from exc
    features = list(
        db.scalars(
            select(FiberTopologyStagedFeature)
            .options(joinedload(FiberTopologyStagedFeature.batch))
            .where(FiberTopologyStagedFeature.id.in_(feature_ids))
        )
        .unique()
        .all()
    )
    by_id = {str(feature.id): feature for feature in features}
    if len(by_id) != len(staged_feature_ids):
        missing = sorted(set(staged_feature_ids) - set(by_id))
        raise FiberTopologyFieldMapError(
            "field-map source cohort changed while reading exact staged geometry: "
            + ", ".join(missing)
        )
    return by_id


def _verify_worklist_row(
    row: dict[str, object], feature: FiberTopologyStagedFeature
) -> None:
    expected: dict[str, object] = {
        "asset_type": feature.asset_type,
        "content_sha256": feature.content_sha256,
        "external_id": feature.external_id,
        "geometry_sha256": feature.geometry_sha256,
        "geometry_type": feature.geometry_type,
        "source_batch_id": str(feature.batch_id),
        "source_profile": feature.batch.profile,
        "source_system": feature.batch.source_system,
        "staged_feature_id": str(feature.id),
    }
    mismatches = [key for key, value in expected.items() if row.get(key) != value]
    if mismatches:
        raise FiberTopologyFieldMapError(
            f"field-map worklist/source mismatch for staged feature {feature.id}: "
            + ", ".join(sorted(mismatches))
        )


def _bbox(points: list[tuple[float, float]]) -> list[float] | None:
    if not points:
        return None
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return [min(longitudes), min(latitudes), max(longitudes), max(latitudes)]


def _map_report(
    db: Session,
    worklist: FiberTopologyFieldWorklistReport,
) -> FiberTopologyFieldMapReport:
    staged_feature_ids = [str(row["staged_feature_id"]) for row in worklist.rows]
    if len(set(staged_feature_ids)) != len(staged_feature_ids):
        raise FiberTopologyFieldMapError(
            "field-verification worklist contains duplicate staged feature identities"
        )
    features_by_id = _features_by_id(db, staged_feature_ids)
    map_features: list[dict[str, object]] = []
    all_points: list[tuple[float, float]] = []
    presentation_states: list[str] = []
    for row in worklist.rows:
        staged_feature_id = str(row["staged_feature_id"])
        feature = features_by_id[staged_feature_id]
        _verify_worklist_row(row, feature)
        geometry, presentation_state, points = _exact_geometry(feature)
        properties = copy.deepcopy(row)
        properties["geometry_presentation_state"] = presentation_state
        map_feature: dict[str, object] = {
            "geometry": geometry,
            "id": staged_feature_id,
            "properties": properties,
            "type": "Feature",
        }
        properties["map_feature_sha256"] = _digest(map_feature)
        map_features.append(map_feature)
        all_points.extend(points)
        presentation_states.append(presentation_state)

    feature_collection: dict[str, object] = {
        "features": map_features,
        "type": "FeatureCollection",
    }
    geometry_presentation_counts = Counter(presentation_states)
    normalized_geometry_counts = {
        state: geometry_presentation_counts[state]
        for state in GEOMETRY_PRESENTATION_STATES
    }
    report_bbox = _bbox(all_points)
    report_payload: dict[str, object] = {
        "asset_type_counts": worklist.asset_type_counts,
        "bbox": report_bbox,
        "current_agreement_count": worklist.current_agreement_count,
        "feature_collection": feature_collection,
        "geometry_presentation_counts": normalized_geometry_counts,
        "needs_follow_up_count": worklist.needs_follow_up_count,
        "priority_counts": worklist.priority_counts,
        "rows_with_current_work_orders": worklist.rows_with_current_work_orders,
        "rows_with_superseded_work_orders": (worklist.rows_with_superseded_work_orders),
        "schema_version": 1,
        "source_batch_count": worklist.source_batch_count,
        "source_profile_counts": worklist.source_profile_counts,
        "source_system_counts": worklist.source_system_counts,
        "staged_feature_count": worklist.staged_feature_count,
        "state_counts": worklist.state_counts,
        "worklist_report_sha256": worklist.report_sha256,
    }
    return FiberTopologyFieldMapReport(
        overlay_sha256=_digest(report_payload),
        worklist_report_sha256=worklist.report_sha256,
        staged_feature_count=worklist.staged_feature_count,
        source_batch_count=worklist.source_batch_count,
        needs_follow_up_count=worklist.needs_follow_up_count,
        current_agreement_count=worklist.current_agreement_count,
        rows_with_current_work_orders=worklist.rows_with_current_work_orders,
        rows_with_superseded_work_orders=worklist.rows_with_superseded_work_orders,
        geometry_presentation_counts=normalized_geometry_counts,
        state_counts=worklist.state_counts,
        priority_counts=worklist.priority_counts,
        asset_type_counts=worklist.asset_type_counts,
        source_system_counts=worklist.source_system_counts,
        source_profile_counts=worklist.source_profile_counts,
        bbox=report_bbox,
        feature_collection=feature_collection,
    )


def project_fiber_field_verification_map(
    db: Session,
) -> FiberTopologyFieldMapReport:
    """Project the complete worklist over exact staged GeoJSON without writes."""

    return _map_report(db, reconcile_fiber_field_worklist(db))


__all__ = [
    "GEOMETRY_PRESENTATION_STATES",
    "FiberTopologyFieldMapError",
    "FiberTopologyFieldMapReport",
    "project_fiber_field_verification_map",
]
