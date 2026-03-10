"""Service helpers for vendor portal routes."""

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models.rbac import Role, SubscriberRole
from app.services import vendor as vendor_service
from app.services import vendor_portal
from app.services.common import coerce_uuid
from app.web.request_parsing import parse_json_body_sync

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
        db.query(SubscriberRole)
        .filter(SubscriberRole.subscriber_id == coerce_uuid(person_id))
        .filter(SubscriberRole.role_id == role.id)
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
    from app.services import web_network_fiber as fiber_service

    map_data = fiber_service.get_fiber_plant_map_data(db)

    return templates.TemplateResponse(
        "vendor/projects/fiber-map.html",
        {
            "request": request,
            "active_page": "my-projects",
            "vendor": context["vendor"],
            "current_user": context["current_user"],
            **map_data,
        },
    )


def vendor_fiber_map_update_position(request: Request, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.models.fiber_change_request import FiberChangeRequestOperation
    from app.services import fiber_change_requests as change_request_service

    def _float_from_obj(value: object | None) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    try:
        data = parse_json_body_sync(request)
        asset_type_obj = data.get("type")
        asset_id_obj = data.get("id")
        latitude = _float_from_obj(data.get("latitude"))
        longitude = _float_from_obj(data.get("longitude"))

        if not isinstance(asset_type_obj, str) or not asset_type_obj:
            return JSONResponse({"error": "Missing required fields"}, status_code=400)
        if not isinstance(asset_id_obj, str) or not asset_id_obj:
            return JSONResponse({"error": "Missing required fields"}, status_code=400)
        if latitude is None or longitude is None:
            return JSONResponse({"error": "Missing required fields"}, status_code=400)

        asset_type = asset_type_obj
        asset_id = asset_id_obj

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


def vendor_fiber_map_nearest_cabinet(request: Request, lat: float, lng: float, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.web.admin import network_fiber_plant as admin_network
    return admin_network.find_nearest_cabinet(request, lat, lng, db)


def vendor_fiber_map_plan_options(request: Request, lat: float, lng: float, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.web.admin import network_fiber_plant as admin_network
    return admin_network.plan_options(request, lat, lng, db)


def vendor_fiber_map_route(request: Request, lat: float, lng: float, cabinet_id: str, db: Session):
    context = _require_vendor_context(request, db)
    if not context:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    if not _has_vendor_role(db, str(context["person"].id), context["vendor_user"].role):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from app.web.admin import network_fiber_plant as admin_network
    return admin_network.plan_route(request, lat, lng, cabinet_id, db)
