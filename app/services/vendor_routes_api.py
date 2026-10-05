"""GeoJSON read service for the native vendor fiber routes (maps §C).

Serves ``proposed_route_revisions.route_geom`` and ``as_built_routes.route_geom``
(LINESTRING, SRID 4326 — ``app/models/vendor_routes.py``) as GeoJSON for the
admin vendor route-view map. Mirrors ``fiber_plant_api``'s ``ST_AsGeoJSON``
pattern: the loaded geometry value is passed back through ``ST_AsGeoJSON`` and
parsed into a GeoJSON geometry dict.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.network import FiberSegment, FiberSpliceClosure
from app.models.vendor_routes import (
    AsBuiltRoute,
    InstallationProject,
    ProjectQuote,
    ProposedRouteRevision,
)
from app.models.work_order import WorkOrder
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def get_active_work_order(db: Session, work_order_id: str | UUID) -> WorkOrder | None:
    return (
        db.query(WorkOrder)
        .filter(WorkOrder.id == coerce_uuid(str(work_order_id)))
        .filter(WorkOrder.is_active.is_(True))
        .one_or_none()
    )


def get_installation_project_for_native_project(
    db: Session, project_id: UUID
) -> InstallationProject | None:
    return (
        db.query(InstallationProject)
        .filter(InstallationProject.project_id == project_id)
        .filter(InstallationProject.is_active.is_(True))
        .one_or_none()
    )


def pending_proposal_names(
    db: Session, asset_type: str, *, provenance_kind: str
) -> set[str]:
    return {
        str((row.payload or {}).get("name") or "")
        for row in (
            db.query(FiberChangeRequest)
            .filter(FiberChangeRequest.asset_type == asset_type)
            .filter(FiberChangeRequest.operation == FiberChangeRequestOperation.create)
            .filter(FiberChangeRequest.status == FiberChangeRequestStatus.pending)
            .all()
        )
        if (row.payload or {}).get("provenance", {}).get("kind") == provenance_kind
    }


def route_name_exists(db: Session, name: str) -> bool:
    return (
        db.query(FiberSegment.id).filter(FiberSegment.name == name).first() is not None
    )


def closure_name_exists(db: Session, name: str) -> bool:
    return (
        db.query(FiberSpliceClosure.id).filter(FiberSpliceClosure.name == name).first()
        is not None
    )


def _geom_to_geojson(db: Session, geom) -> dict | None:
    """Convert a loaded geometry value to a GeoJSON geometry dict."""
    if geom is None:
        return None
    try:
        raw = db.query(func.ST_AsGeoJSON(geom)).scalar()
    except Exception:  # pragma: no cover - defensive (non-PostGIS backends)
        logger.warning("ST_AsGeoJSON failed for route geometry", exc_info=True)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _project_label(project: InstallationProject | None) -> str:
    if project is None:
        return "Unknown project"
    native = getattr(project, "project", None)
    if native is not None and getattr(native, "name", None):
        return native.name
    if project.procurement_order_reference:
        return project.procurement_order_reference
    return f"Project {str(project.id)[:8]}"


def _closure_proposal_features(
    db: Session,
    project: InstallationProject,
    *,
    vendor_id: UUID | None = None,
) -> list[dict]:
    """Project review-gated closure pins for one installation project."""

    work_order_ids = {
        str(row[0])
        for row in db.query(WorkOrder.id)
        .filter(WorkOrder.project_id == project.project_id)
        .all()
    }
    query = db.query(FiberChangeRequest).filter(
        FiberChangeRequest.asset_type == "splice_closure"
    )
    if vendor_id is not None:
        query = query.filter(FiberChangeRequest.requested_by_vendor_id == vendor_id)
    features: list[dict] = []
    for request in query.order_by(FiberChangeRequest.created_at.asc()).all():
        payload = request.payload or {}
        provenance = payload.get("provenance") or {}
        project_scoped = str(provenance.get("installation_project_id") or "") == str(
            project.id
        )
        work_order_scoped = str(provenance.get("work_order_id") or "") in work_order_ids
        if not project_scoped and not work_order_scoped:
            continue
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        if latitude is None or longitude is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(longitude), float(latitude)],
                },
                "properties": {
                    "id": str(request.id),
                    "kind": "closure_proposal",
                    "status": request.status.value,
                    "name": payload.get("name") or "Proposed closure",
                    "notes": payload.get("notes"),
                    "work_order_id": provenance.get("work_order_public_id"),
                    "review_notes": request.review_notes,
                    "review_url": f"/admin/network/fiber-change-requests/{request.id}",
                },
            }
        )
    return features


def _admin_route_proposal_features(
    db: Session,
    project: InstallationProject | None = None,
) -> list[dict]:
    """Project-scoped staff route proposals awaiting fibre review."""

    features: list[dict] = []
    requests = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_segment")
        .filter(FiberChangeRequest.operation == FiberChangeRequestOperation.create)
        .order_by(FiberChangeRequest.created_at.asc())
        .all()
    )
    for request in requests:
        payload = request.payload or {}
        provenance = payload.get("provenance") or {}
        if provenance.get("kind") != "admin_route":
            continue
        if project is not None and str(
            provenance.get("installation_project_id") or ""
        ) != str(project.id):
            continue
        geometry = payload.get("geojson") or payload.get("route_geom")
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(request.id),
                    "kind": "admin_route_proposal",
                    "name": payload.get("name") or "Suggested route",
                    "status": request.status.value,
                    "length_meters": payload.get("length_meters")
                    or payload.get("length_m"),
                    "review_notes": request.review_notes,
                    "review_url": f"/admin/network/fiber-change-requests/{request.id}",
                },
            }
        )
    return features


def build_admin_proposal_geojson(db: Session) -> dict:
    """All standalone admin route and closure proposals for the authoring map."""

    features = _admin_route_proposal_features(db)
    requests = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "splice_closure")
        .filter(FiberChangeRequest.operation == FiberChangeRequestOperation.create)
        .order_by(FiberChangeRequest.created_at.asc())
        .all()
    )
    for request in requests:
        payload = request.payload or {}
        provenance = payload.get("provenance") or {}
        if provenance.get("kind") != "admin_map_asset":
            continue
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        if latitude is None or longitude is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(longitude), float(latitude)],
                },
                "properties": {
                    "id": str(request.id),
                    "kind": "closure_proposal",
                    "name": payload.get("name") or "Proposed closure",
                    "status": request.status.value,
                    "review_notes": request.review_notes,
                    "review_url": f"/admin/network/fiber-change-requests/{request.id}",
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def build_project_route_geojson(db: Session, project_id: str) -> dict:
    """All proposed + as-built routes for an installation project as GeoJSON.

    Returns a ``FeatureCollection``; each feature carries a ``kind`` property of
    ``proposed`` or ``as_built`` plus revision/status metadata.
    """
    features: list[dict] = []
    project = db.get(InstallationProject, coerce_uuid(project_id))
    if project is None:
        return {"type": "FeatureCollection", "features": []}

    revisions = (
        db.query(ProposedRouteRevision)
        .join(ProjectQuote, ProposedRouteRevision.quote_id == ProjectQuote.id)
        .filter(ProjectQuote.project_id == project_id)
        .filter(ProposedRouteRevision.route_geom.isnot(None))
        .order_by(ProposedRouteRevision.revision_number.asc())
        .all()
    )
    for revision in revisions:
        geometry = _geom_to_geojson(db, revision.route_geom)
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(revision.id),
                    "kind": "proposed",
                    "quote_id": str(revision.quote_id),
                    "revision_number": revision.revision_number,
                    "status": revision.status,
                    "length_meters": revision.length_meters,
                },
            }
        )

    as_builts = (
        db.query(AsBuiltRoute)
        .filter(AsBuiltRoute.project_id == project_id)
        .filter(AsBuiltRoute.route_geom.isnot(None))
        .order_by(AsBuiltRoute.version.asc())
        .all()
    )
    for as_built in as_builts:
        geometry = _geom_to_geojson(db, as_built.route_geom)
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(as_built.id),
                    "kind": "as_built",
                    "status": as_built.status,
                    "version": as_built.version,
                    "length_meters": as_built.actual_length_meters,
                    "variation_type": as_built.variation_type,
                },
            }
        )

    features.extend(_admin_route_proposal_features(db, project))
    features.extend(_closure_proposal_features(db, project))

    return {"type": "FeatureCollection", "features": features}


def build_as_built_route_geojson(db: Session, as_built_id: str) -> dict:
    """One as-built submission as a permission-scoped review map."""

    row = db.query(AsBuiltRoute).filter(AsBuiltRoute.id == as_built_id).one_or_none()
    if row is None:
        return {"type": "FeatureCollection", "features": []}
    geometry = _geom_to_geojson(db, row.route_geom)
    if geometry is None:
        return {"type": "FeatureCollection", "features": []}
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(row.id),
                    "kind": "as_built",
                    "status": row.status,
                    "version": row.version,
                    "length_meters": row.actual_length_meters,
                    "variation_type": row.variation_type,
                },
            }
        ],
    }


def build_vendor_project_route_geojson(
    db: Session,
    project_id: str,
    vendor_id: str,
) -> dict:
    """Vendor-scoped proposed and as-built route context for one project.

    Bidding vendors may only see route revisions attached to their own quote.
    As-built evidence is visible only when the project is assigned to the same
    vendor. Admin route views continue to use ``build_project_route_geojson``.
    """

    project_uuid = coerce_uuid(project_id)
    vendor_uuid = coerce_uuid(vendor_id)
    features: list[dict] = []
    project = db.get(InstallationProject, project_uuid)
    if project is None:
        return {"type": "FeatureCollection", "features": []}

    revisions = (
        db.query(ProposedRouteRevision)
        .join(ProjectQuote, ProposedRouteRevision.quote_id == ProjectQuote.id)
        .filter(ProjectQuote.project_id == project_uuid)
        .filter(ProjectQuote.vendor_id == vendor_uuid)
        .filter(ProjectQuote.is_active.is_(True))
        .order_by(ProposedRouteRevision.revision_number.asc())
        .all()
    )
    for revision in revisions:
        geometry = _geom_to_geojson(db, revision.route_geom)
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(revision.id),
                    "kind": "proposed",
                    "quote_id": str(revision.quote_id),
                    "revision_number": revision.revision_number,
                    "status": revision.status,
                    "length_meters": revision.length_meters,
                },
            }
        )

    as_builts = (
        db.query(AsBuiltRoute)
        .join(
            InstallationProject,
            AsBuiltRoute.project_id == InstallationProject.id,
        )
        .filter(AsBuiltRoute.project_id == project_uuid)
        .filter(InstallationProject.assigned_vendor_id == vendor_uuid)
        .order_by(AsBuiltRoute.version.asc())
        .all()
    )
    for as_built in as_builts:
        geometry = _geom_to_geojson(db, as_built.route_geom)
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(as_built.id),
                    "kind": "as_built",
                    "status": as_built.status,
                    "version": as_built.version,
                    "length_meters": as_built.actual_length_meters,
                    "variation_type": as_built.variation_type,
                },
            }
        )

    features.extend(_closure_proposal_features(db, project, vendor_id=vendor_uuid))

    return {"type": "FeatureCollection", "features": features}


def get_route_project(db: Session, project_id: str) -> dict | None:
    """Summary of a single installation project for the route-view page header."""
    project = (
        db.query(InstallationProject)
        .filter(InstallationProject.id == project_id)
        .one_or_none()
    )
    if project is None:
        return None
    vendor = getattr(project, "assigned_vendor", None)
    return {
        "id": str(project.id),
        "label": _project_label(project),
        "status": project.status,
        "vendor": vendor.name if vendor is not None else None,
        "native_project_id": str(project.project_id),
    }


def list_admin_route_proposals(
    db: Session, project_id: str | None = None
) -> list[dict]:
    """Return staff-owned route proposals, optionally filtered by project."""

    rows = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_segment")
        .filter(FiberChangeRequest.operation == FiberChangeRequestOperation.create)
        .order_by(FiberChangeRequest.created_at.desc())
        .all()
    )
    items: list[dict] = []
    for row in rows:
        payload = row.payload or {}
        provenance = payload.get("provenance") or {}
        if provenance.get("kind") != "admin_route":
            continue
        if project_id is not None and str(
            provenance.get("installation_project_id") or ""
        ) != str(project_id):
            continue
        items.append(
            {
                "id": str(row.id),
                "name": payload.get("name") or "Suggested route",
                "status": row.status.value,
                "length_meters": payload.get("length_meters")
                or payload.get("length_m"),
                "created_at": row.created_at,
                "review_notes": row.review_notes,
                "review_url": f"/admin/network/fiber-change-requests/{row.id}",
            }
        )
    return items


def list_admin_authoring_projects(db: Session) -> list[dict]:
    """Active project choices for optional proposal provenance."""

    rows = (
        db.query(InstallationProject)
        .filter(InstallationProject.is_active.is_(True))
        .order_by(InstallationProject.created_at.desc(), InstallationProject.id.desc())
        .all()
    )
    return [
        {
            "id": str(row.id),
            "label": _project_label(row),
            "status": row.status,
            "native_project_id": str(row.project_id),
        }
        for row in rows
    ]


def list_admin_authoring_work_orders(db: Session) -> list[dict]:
    """Active work-order choices for optional proposal provenance."""

    rows = (
        db.query(WorkOrder)
        .filter(WorkOrder.is_active.is_(True))
        .order_by(WorkOrder.created_at.desc(), WorkOrder.id.desc())
        .all()
    )
    return [
        {
            "id": str(row.id),
            "public_id": row.public_id,
            "title": row.title,
            "project_id": str(row.project_id) if row.project_id else None,
        }
        for row in rows
    ]


def list_route_projects(db: Session) -> list[dict]:
    """Installation projects that carry route geometry or closure proposals."""
    proposed_project_ids = {
        row[0]
        for row in (
            db.query(ProjectQuote.project_id)
            .join(
                ProposedRouteRevision,
                ProposedRouteRevision.quote_id == ProjectQuote.id,
            )
            .filter(ProposedRouteRevision.route_geom.isnot(None))
            .distinct()
            .all()
        )
    }
    admin_proposed_project_ids: set[UUID] = set()
    for row in (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_segment")
        .filter(FiberChangeRequest.operation == FiberChangeRequestOperation.create)
        .all()
    ):
        provenance = (row.payload or {}).get("provenance") or {}
        if provenance.get("kind") != "admin_route":
            continue
        try:
            admin_proposed_project_ids.add(
                UUID(str(provenance["installation_project_id"]))
            )
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "Ignoring admin route proposal %s with invalid project provenance",
                row.id,
            )
    as_built_project_ids = {
        row[0]
        for row in (
            db.query(AsBuiltRoute.project_id)
            .filter(AsBuiltRoute.route_geom.isnot(None))
            .distinct()
            .all()
        )
    }
    closure_project_ids: set[UUID] = set()
    closure_requests = (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "splice_closure")
        .all()
    )
    closure_work_order_ids: set[UUID] = set()
    for request in closure_requests:
        provenance = (request.payload or {}).get("provenance") or {}
        if provenance.get("kind") == "admin_map_asset":
            try:
                closure_project_ids.add(
                    UUID(str(provenance["installation_project_id"]))
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Ignoring admin asset proposal %s with invalid project provenance",
                    request.id,
                )
            continue
        work_order_id = provenance.get("work_order_id")
        if not work_order_id:
            continue
        try:
            closure_work_order_ids.add(UUID(str(work_order_id)))
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring closure proposal %s with invalid work-order provenance",
                request.id,
            )
    if closure_work_order_ids:
        native_project_ids = {
            row[0]
            for row in db.query(WorkOrder.project_id)
            .filter(WorkOrder.id.in_(closure_work_order_ids))
            .distinct()
            .all()
        }
        if native_project_ids:
            closure_project_ids = {
                row[0]
                for row in db.query(InstallationProject.id)
                .filter(InstallationProject.project_id.in_(native_project_ids))
                .all()
            }
    project_ids = (
        proposed_project_ids
        | admin_proposed_project_ids
        | as_built_project_ids
        | closure_project_ids
    )
    if not project_ids:
        return []

    projects = (
        db.query(InstallationProject)
        .filter(InstallationProject.id.in_(project_ids))
        .all()
    )
    items = [
        {
            "id": str(project.id),
            "label": _project_label(project),
            "status": project.status,
            "vendor": (
                project.assigned_vendor.name
                if getattr(project, "assigned_vendor", None) is not None
                else None
            ),
            "has_proposed": project.id in proposed_project_ids,
            "has_admin_proposed": project.id in admin_proposed_project_ids,
            "has_as_built": project.id in as_built_project_ids,
            "has_closure_proposals": project.id in closure_project_ids,
        }
        for project in projects
    ]
    items.sort(key=lambda item: str(item["label"]).lower())
    return items
