"""Admin GIS web routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import gis as gis_service
from app.services import web_gis as web_gis_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/gis", tags=["web-admin-gis"])


def _base_context(request: Request, db: Session) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "gis",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("", response_class=HTMLResponse)
def gis_index(request: Request, tab: str = "locations", db: Session = Depends(get_db)):
    context = _base_context(request, db)
    context.update(web_gis_service.list_page_data(db, tab=tab))
    return templates.TemplateResponse("admin/gis/index.html", context)


@router.get("/locations/new", response_class=HTMLResponse)
def gis_location_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db)
    context.update(
        web_gis_service.build_location_form_context(
            location=None,
            action_url="/admin/gis/locations/new",
        )
    )
    return templates.TemplateResponse("admin/gis/location_form.html", context)


@router.post("/locations/new", response_class=HTMLResponse)
def gis_location_create(
    request: Request,
    name: str = Form(...),
    location_type: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    notes: str = Form(None),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = web_gis_service.build_location_create_payload(
            name=name,
            location_type=location_type,
            latitude=latitude,
            longitude=longitude,
            notes=notes,
            is_active=is_active,
        )
        gis_service.geo_locations.create(db=db, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)
    except Exception as e:
        context = _base_context(request, db)
        context.update(
            web_gis_service.build_location_form_context(
                location=None,
                action_url="/admin/gis/locations/new",
                error=str(e),
            )
        )
        return templates.TemplateResponse("admin/gis/location_form.html", context)


@router.get("/locations/{location_id}/edit", response_class=HTMLResponse)
def gis_location_edit(request: Request, location_id: str, db: Session = Depends(get_db)):
    location = gis_service.geo_locations.get(db=db, location_id=location_id)

    context = _base_context(request, db)
    context.update(
        web_gis_service.build_location_form_context(
            location=location,
            action_url=f"/admin/gis/locations/{location_id}/edit",
        )
    )
    return templates.TemplateResponse("admin/gis/location_form.html", context)


@router.post("/locations/{location_id}/edit", response_class=HTMLResponse)
def gis_location_update(
    request: Request,
    location_id: str,
    name: str = Form(...),
    location_type: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    notes: str = Form(None),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = web_gis_service.build_location_update_payload(
            name=name,
            location_type=location_type,
            latitude=latitude,
            longitude=longitude,
            notes=notes,
            is_active=is_active,
        )
        gis_service.geo_locations.update(db=db, location_id=location_id, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)
    except Exception as e:
        location = gis_service.geo_locations.get(db=db, location_id=location_id)
        context = _base_context(request, db)
        context.update(
            web_gis_service.build_location_form_context(
                location=location,
                action_url=f"/admin/gis/locations/{location_id}/edit",
                error=str(e),
            )
        )
        return templates.TemplateResponse("admin/gis/location_form.html", context)
