"""Service helpers for vendor portal routes."""

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.services import vendor as vendor_service
from app.services import vendor_portal
from app.models.rbac import PersonRole, Role

templates = Jinja2Templates(directory="templates")

_VENDOR_ROLE_NAME = "vendors"


def _require_vendor_context(request: Request, db: Session):
    context = vendor_portal.get_context(
        db, request.cookies.get(vendor_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    return context


def _has_vendor_role(db: Session, person_id: str, vendor_role: str | None) -> bool:
    if vendor_role and vendor_role.strip().lower() == _VENDOR_ROLE_NAME:
        return True
    role = db.query(Role).filter(Role.name.ilike(_VENDOR_ROLE_NAME)).first()
    if not role:
        return False
    return (
        db.query(PersonRole)
        .filter(PersonRole.person_id == person_id)
        .filter(PersonRole.role_id == role.id)
        .first()
        is not None
    )


def vendor_home(request: Request, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    return RedirectResponse(url="/vendor/dashboard", status_code=303)


def vendor_dashboard(request: Request, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    vendor_id = str(context["vendor"].id)
    available = vendor_service.installation_projects.list_available_for_vendor(
        db, vendor_id, limit=10, offset=0
    )
    mine = vendor_service.installation_projects.list_for_vendor(
        db, vendor_id, limit=10, offset=0
    )
    return templates.TemplateResponse(
        "vendor/dashboard/index.html",
        {
            "request": request,
            "active_page": "dashboard",
            "vendor": context["vendor"],
            "current_user": context["current_user"],
            "available_projects": available,
            "my_projects": mine,
        },
    )


def vendor_projects_available(request: Request, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    vendor_id = str(context["vendor"].id)
    projects = vendor_service.installation_projects.list_available_for_vendor(
        db, vendor_id, limit=50, offset=0
    )
    return templates.TemplateResponse(
        "vendor/projects/available.html",
        {
            "request": request,
            "active_page": "available-projects",
            "vendor": context["vendor"],
            "current_user": context["current_user"],
            "projects": projects,
        },
    )


def vendor_projects_mine(request: Request, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    vendor_id = str(context["vendor"].id)
    projects = vendor_service.installation_projects.list_for_vendor(
        db, vendor_id, limit=50, offset=0
    )
    return templates.TemplateResponse(
        "vendor/projects/my-projects.html",
        {
            "request": request,
        "active_page": "fiber-map",
            "vendor": context["vendor"],
            "current_user": context["current_user"],
            "projects": projects,
        },
    )


def quote_builder(request: Request, project_id: str, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    project = vendor_service.installation_projects.get(db, project_id)
    return templates.TemplateResponse(
        "vendor/quotes/builder.html",
        {
            "request": request,
            "active_page": "quote-builder",
            "vendor": context["vendor"],
            "current_user": context["current_user"],
            "project": project,
        },
    )


def as_built_submit(request: Request, project_id: str, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    project = vendor_service.installation_projects.get(db, project_id)
    return templates.TemplateResponse(
        "vendor/as-built/submit.html",
        {
            "request": request,
            "active_page": "as-built",
            "vendor": context["vendor"],
            "current_user": context["current_user"],
            "project": project,
        },
    )


def vendor_fiber_map(request: Request, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return HTMLResponse(content="Forbidden", status_code=403)

    import json
    from sqlalchemy import func
    from app.models.network import FdhCabinet, FiberSpliceClosure, FiberSegment, Splitter, FiberSplice, FiberSpliceTray
    from app.services import settings_spec
    from app.models.domain_settings import SettingDomain

    features = []

    # FDH Cabinets
    fdh_cabinets = db.query(FdhCabinet).filter(
        FdhCabinet.is_active.is_(True),
        FdhCabinet.latitude.isnot(None),
        FdhCabinet.longitude.isnot(None)
    ).all()
    splitter_counts = {}
    if fdh_cabinets:
        fdh_ids = [fdh.id for fdh in fdh_cabinets]
        splitter_counts = dict(
            db.query(Splitter.fdh_id, func.count(Splitter.id))
            .filter(Splitter.fdh_id.in_(fdh_ids))
            .group_by(Splitter.fdh_id)
            .all()
        )
    for fdh in fdh_cabinets:
        splitter_count = splitter_counts.get(fdh.id, 0)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
            "properties": {
                "id": str(fdh.id),
                "type": "fdh_cabinet",
                "name": fdh.name,
                "code": fdh.code,
                "splitter_count": splitter_count,
            },
        })

    # Splice Closures
    closures = db.query(FiberSpliceClosure).filter(
        FiberSpliceClosure.is_active.is_(True),
        FiberSpliceClosure.latitude.isnot(None),
        FiberSpliceClosure.longitude.isnot(None)
    ).all()
    splice_counts = {}
    tray_counts = {}
    if closures:
        closure_ids = [closure.id for closure in closures]
        splice_counts = dict(
            db.query(FiberSplice.closure_id, func.count(FiberSplice.id))
            .filter(FiberSplice.closure_id.in_(closure_ids))
            .group_by(FiberSplice.closure_id)
            .all()
        )
        tray_counts = dict(
            db.query(FiberSpliceTray.closure_id, func.count(FiberSpliceTray.id))
            .filter(FiberSpliceTray.closure_id.in_(closure_ids))
            .group_by(FiberSpliceTray.closure_id)
            .all()
        )
    for closure in closures:
        splice_count = splice_counts.get(closure.id, 0)
        tray_count = tray_counts.get(closure.id, 0)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [closure.longitude, closure.latitude]},
            "properties": {
                "id": str(closure.id),
                "type": "splice_closure",
                "name": closure.name,
                "splice_count": splice_count,
                "tray_count": tray_count,
            },
        })

    # Fiber Segments
    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_geoms = db.query(FiberSegment, func.ST_AsGeoJSON(FiberSegment.route_geom)).filter(
        FiberSegment.is_active.is_(True),
        FiberSegment.route_geom.isnot(None),
    ).all()
    for segment, geojson_str in segment_geoms:
        if not geojson_str:
            continue
        geom = json.loads(geojson_str)
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id": str(segment.id),
                "type": "fiber_segment",
                "name": segment.name,
                "segment_type": segment.segment_type.value if segment.segment_type else None,
                "cable_type": segment.cable_type.value if segment.cable_type else None,
                "fiber_count": segment.fiber_count,
                "length_m": segment.length_m,
            },
        })

    geojson_data = {"type": "FeatureCollection", "features": features}

    stats = {
        "fdh_cabinets": db.query(func.count(FdhCabinet.id)).filter(FdhCabinet.is_active.is_(True)).scalar(),
        "fdh_with_location": len(fdh_cabinets),
        "splice_closures": db.query(func.count(FiberSpliceClosure.id)).filter(FiberSpliceClosure.is_active.is_(True)).scalar(),
        "closures_with_location": len(closures),
        "splitters": db.query(func.count(Splitter.id)).filter(Splitter.is_active.is_(True)).scalar(),
        "total_splices": db.query(func.count(FiberSplice.id)).scalar(),
        "segments": len(segments),
    }

    cost_settings = {
        "drop_cable_per_meter": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_drop_cable_cost_per_meter") or "2.50"),
        "labor_per_meter": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_labor_cost_per_meter") or "1.50"),
        "ont_device": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_ont_device_cost") or "85.00"),
        "installation_base": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_installation_base_fee") or "50.00"),
        "currency": settings_spec.resolve_value(db, SettingDomain.billing, "default_currency") or "NGN",
    }

    return templates.TemplateResponse(
        "vendor/projects/fiber-map.html",
        {
            "request": request,
            "active_page": "my-projects",
            "vendor": context["vendor"],
            "current_user": context["current_user"],
            "geojson_data": geojson_data,
            "stats": stats,
            "cost_settings": cost_settings,
        },
    )


async def vendor_fiber_map_update_position(request: Request, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.services import fiber_change_requests as change_request_service
    from app.models.fiber_change_request import FiberChangeRequestOperation

    try:
        data = await request.json()
        asset_type = data.get("type")
        asset_id = data.get("id")
        latitude = data.get("latitude")
        longitude = data.get("longitude")

        if not all([asset_type, asset_id, latitude is not None, longitude is not None]):
            return JSONResponse({"error": "Missing required fields"}, status_code=400)

        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid coordinates"}, status_code=400)

        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return JSONResponse({"error": "Coordinates out of range"}, status_code=400)

        request_record = change_request_service.create_request(
            db,
            asset_type=asset_type,
            asset_id=asset_id,
            operation=FiberChangeRequestOperation.update,
            payload={"latitude": latitude, "longitude": longitude},
            requested_by_person_id=str(context["person"].id),
            requested_by_vendor_id=str(context["vendor"].id),
        )

        return JSONResponse(
            {
                "success": True,
                "request_id": str(request_record.id),
                "status": request_record.status.value,
            }
        )
    except Exception as exc:
        db.rollback()
        return JSONResponse({"error": str(exc)}, status_code=500)


async def vendor_fiber_map_nearest_cabinet(request: Request, lat: float, lng: float, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.web.admin import network as admin_network
    return await admin_network.find_nearest_cabinet(request, lat, lng, db)


async def vendor_fiber_map_plan_options(request: Request, lat: float, lng: float, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.web.admin import network as admin_network
    return await admin_network.plan_options(request, lat, lng, db)


async def vendor_fiber_map_route(request: Request, lat: float, lng: float, cabinet_id: str, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.web.admin import network as admin_network
    return await admin_network.plan_route(request, lat, lng, cabinet_id, db)
