"""Admin vendor route-view web routes (maps §C).

UI-only port of the CRM ``vendors/quotes/route-view`` page: renders the native
vendor ``route_geom`` (proposed + as-built) over the fiber-plant network on
Leaflet. Geometry is fetched client-side from the ``/api/v1/vendor-routes``
GeoJSON endpoint; the fiber overlay reuses ``fiber_plant_api``. Guarded by
``network:fiber:read`` — consistent with the fiber map and the GeoJSON API.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from json import JSONDecodeError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.fiber_change_request import FiberChangeRequestOperation
from app.models.work_order import WorkOrder
from app.schemas.vendor_portal import VendorRouteRevisionCreate
from app.services import (
    fiber_change_requests,
    fiber_plant_api,
    vendor_routes_api,
    work_order_views,
)
from app.services.auth_dependencies import can, require_permission
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.vendor_portal_operations import vendor_portal_operations
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendors", tags=["web-admin-vendor-routes"])


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", {}) or {}
    return str(auth.get("principal_id") or auth.get("person_id") or "").strip()


def _requester_person_id(request: Request) -> str | None:
    """Return a subscriber/person FK only when the authenticated principal has one."""

    auth = getattr(request.state, "auth", {}) or {}
    if auth.get("principal_type") == "system_user":
        return None
    value = str(auth.get("person_id") or "").strip()
    return value or None


def _route_revision_payload(form: Mapping[str, object]) -> VendorRouteRevisionCreate:
    raw_geojson = str(form.get("geojson") or "").strip()
    raw_length = str(form.get("length_meters") or "").strip()
    try:
        return VendorRouteRevisionCreate(
            geojson=json.loads(raw_geojson),
            length_meters=float(raw_length) if raw_length else None,
        )
    except (JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Trace a valid route with at least two map points.",
        ) from exc


def _form_uuid(value: object, detail: str):
    try:
        return coerce_uuid(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=detail) from exc


def _redirect(project_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/vendors/routes/{project_id}?message={message}", status_code=303
    )


def _authoring_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/vendors/routes/new?message={message}", status_code=303
    )


def _optional_scope(
    db: Session,
    *,
    project_id: str | None,
    work_order_id: str | None,
) -> tuple[dict[str, object] | None, WorkOrder | None]:
    """Resolve optional project/work-order provenance without requiring either."""

    project = None
    if project_id:
        project = vendor_routes_api.get_route_project(db, project_id)
        if project is None:
            raise HTTPException(
                status_code=404, detail="Installation project not found"
            )

    work_order = None
    if work_order_id:
        work_order = vendor_routes_api.get_active_work_order(
            db, _form_uuid(work_order_id, "Invalid work order")
        )
        if work_order is None:
            raise HTTPException(status_code=404, detail="Work order not found")
        if project is not None and str(work_order.project_id) != str(
            project["native_project_id"]
        ):
            raise HTTPException(
                status_code=422,
                detail="The work order does not belong to the selected project",
            )
        if project is None and work_order.project_id is not None:
            installation = (
                vendor_routes_api.get_installation_project_for_native_project(
                    db, work_order.project_id
                )
            )
            if installation is not None:
                project = vendor_routes_api.get_route_project(db, str(installation.id))
    return project, work_order


def _ctx(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/routes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def vendor_routes_list(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "vendor-routes")
    context["projects"] = vendor_routes_api.list_route_projects(db)
    return templates.TemplateResponse("admin/vendors/routes.html", context)


@router.post(
    "/routes/suggested-route",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def create_standalone_admin_suggested_route(
    request: Request,
    db: Session = Depends(get_db),
):
    """Submit an admin route proposal without requiring project context."""

    _create_admin_route_request(request, parse_form_data_sync(request), db)
    return _authoring_redirect("Suggested route submitted for review")


@router.post(
    "/routes/{project_id}/suggested-route",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def create_admin_suggested_route(
    request: Request,
    project_id: str,
    db: Session = Depends(get_db),
):
    """Submit a staff-owned route proposal from a project context."""

    form = parse_form_data_sync(request)
    _create_admin_route_request(request, form, db, default_project_id=project_id)
    return _redirect(project_id, "Suggested route submitted for review")


def _create_admin_route_request(
    request: Request,
    form: Mapping[str, object],
    db: Session,
    *,
    default_project_id: str | None = None,
) -> None:
    actor = _actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Authenticated actor is required")
    project_id = str(form.get("project_id") or "").strip() or default_project_id
    project, work_order = _optional_scope(
        db,
        project_id=project_id,
        work_order_id=str(form.get("work_order_id") or "").strip() or None,
    )
    route_name = str(form.get("name") or "").strip()
    if not route_name:
        raise HTTPException(status_code=422, detail="A route name is required")
    if len(route_name) > 160:
        raise HTTPException(status_code=422, detail="The route name is too long")
    payload = _route_revision_payload(form)
    actor_id = _form_uuid(actor, "Authenticated actor is required")
    pending_names = vendor_routes_api.pending_proposal_names(
        db, "fiber_segment", provenance_kind="admin_route"
    )
    if vendor_routes_api.route_name_exists(db, route_name):
        raise HTTPException(
            status_code=409, detail="A route with this name already exists"
        )
    if route_name in pending_names:
        raise HTTPException(
            status_code=409,
            detail="A route with this name is already awaiting review",
        )
    provenance: dict[str, object] = {
        "kind": "admin_route",
        "person_id": str(actor_id),
    }
    route_payload: dict[str, object] = {
        "name": route_name,
        "geojson": payload.geojson,
        "length_m": payload.length_meters,
        "is_active": False,
        "notes": str(form.get("notes") or "").strip()[:2000] or None,
        "provenance": provenance,
    }
    if project is not None:
        provenance.update(
            {
                "installation_project_id": str(project["id"]),
                "native_project_id": str(project["native_project_id"]),
            }
        )
    if work_order is not None:
        provenance.update(
            {
                "work_order_id": str(work_order.id),
                "work_order_public_id": work_order.public_id,
            }
        )
    try:
        db_session_adapter.release_read_transaction(db)
        fiber_change_requests.create_request(
            db,
            asset_type="fiber_segment",
            asset_id=None,
            operation=FiberChangeRequestOperation.create,
            payload=route_payload,
            requested_by_person_id=_requester_person_id(request),
            requested_by_vendor_id=None,
        )
    except (DomainError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post(
    "/routes/asset-proposals",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def create_standalone_admin_asset_proposal(
    request: Request,
    db: Session = Depends(get_db),
):
    """Submit an admin asset proposal without requiring project context."""

    _create_admin_asset_proposal(request, parse_form_data_sync(request), db)
    return _authoring_redirect("Asset proposal submitted for review")


@router.post(
    "/routes/{project_id}/asset-proposals",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def create_admin_asset_proposal(
    request: Request,
    project_id: str,
    db: Session = Depends(get_db),
):
    """Pin a review-gated closure from a project context."""

    form = parse_form_data_sync(request)
    _create_admin_asset_proposal(request, form, db, default_project_id=project_id)
    return _redirect(project_id, "Asset proposal submitted for review")


def _create_admin_asset_proposal(
    request: Request,
    form: Mapping[str, object],
    db: Session,
    *,
    default_project_id: str | None = None,
) -> None:
    actor = _actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Authenticated actor is required")
    project_id = str(form.get("project_id") or "").strip() or default_project_id
    project, work_order = _optional_scope(
        db,
        project_id=project_id,
        work_order_id=str(form.get("work_order_id") or "").strip() or None,
    )
    latitude_raw = str(form.get("latitude") or "").strip()
    longitude_raw = str(form.get("longitude") or "").strip()
    try:
        latitude = float(latitude_raw)
        longitude = float(longitude_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="Valid map coordinates are required"
        ) from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise HTTPException(status_code=422, detail="Map coordinates are out of range")
    name = str(form.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="An asset name is required")
    if len(name) > 160:
        raise HTTPException(status_code=422, detail="Asset name is too long")
    if vendor_routes_api.closure_name_exists(db, name):
        raise HTTPException(
            status_code=409, detail="A closure with this name already exists"
        )
    pending_names = vendor_routes_api.pending_proposal_names(
        db, "splice_closure", provenance_kind="admin_map_asset"
    )
    if name in pending_names:
        raise HTTPException(
            status_code=409,
            detail="A closure with this name is already awaiting review",
        )
    actor_id = _form_uuid(actor, "Authenticated actor is required")
    provenance: dict[str, object] = {
        "kind": "admin_map_asset",
        "person_id": str(actor_id),
    }
    payload: dict[str, object] = {
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "geom": {"type": "Point", "coordinates": [longitude, latitude]},
        "is_active": False,
        "provenance": provenance,
    }
    if project is not None:
        provenance.update(
            {
                "installation_project_id": str(project["id"]),
                "native_project_id": str(project["native_project_id"]),
            }
        )
    if work_order is not None:
        provenance.update(
            {
                "work_order_id": str(work_order.id),
                "work_order_public_id": work_order.public_id,
            }
        )
    notes = str(form.get("notes") or "").strip()
    if notes:
        payload["notes"] = notes[:2000]
    fiber_change_requests.create_request(
        db,
        asset_type="splice_closure",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload=payload,
        requested_by_person_id=_requester_person_id(request),
        requested_by_vendor_id=None,
    )


@router.get(
    "/routes/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def admin_route_authoring(
    request: Request,
    message: str | None = None,
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "vendor-routes")
    context.update(
        {
            "message": message,
            "can_write_routes": can(request, "network:fiber:write"),
            "projects": vendor_routes_api.list_admin_authoring_projects(db),
            "work_orders": vendor_routes_api.list_admin_authoring_work_orders(db),
            "route_geojson": vendor_routes_api.build_admin_proposal_geojson(db),
            "network_geojson": fiber_plant_api.build_fiber_plant_geojson(
                db,
                include_fdh=True,
                include_closures=True,
                include_pops=True,
                include_segments=True,
            ),
        }
    )
    return templates.TemplateResponse("admin/vendors/route_authoring.html", context)


@router.get(
    "/routes/{project_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def vendor_route_view(
    request: Request,
    project_id: str,
    revision_id: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
):
    project = vendor_routes_api.get_route_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Installation project not found")
    context = _ctx(request, db, "vendor-routes")
    context.update(
        {
            "project": project,
            "project_id": project_id,
            "revision_id": revision_id,
            "message": message,
            "can_write_routes": can(request, "network:fiber:write"),
            "admin_route_proposals": vendor_routes_api.list_admin_route_proposals(
                db, project_id
            ),
            "work_orders": work_order_views.list_project_work_order_summaries(
                db,
                coerce_uuid(project["native_project_id"]),
            ),
            "route_geojson": vendor_routes_api.build_project_route_geojson(
                db, project_id
            ),
            "route_revisions": (
                vendor_portal_operations.list_route_revisions_for_project(
                    db,
                    project_id,
                )
            ),
            "network_geojson": fiber_plant_api.build_fiber_plant_geojson(
                db,
                include_fdh=True,
                include_closures=True,
                include_pops=True,
                include_segments=True,
            ),
        }
    )
    return templates.TemplateResponse("admin/vendors/route_view.html", context)
