"""Admin GIS web routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.gis import GeoAreaType, GeoLocationType
from app.schemas.gis import GeoLocationCreate, GeoLocationUpdate
from app.services import gis as gis_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/gis", tags=["web-admin-gis"])


@router.get("", response_class=HTMLResponse)
def gis_index(request: Request, tab: str = "locations", db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    # Get GIS data
    locations = gis_service.geo_locations.list(
        db=db, location_type=None, address_id=None, pop_site_id=None, is_active=None,
        min_latitude=None, min_longitude=None, max_latitude=None, max_longitude=None,
        order_by="created_at", order_dir="desc", limit=100, offset=0
    )
    areas = gis_service.geo_areas.list(
        db=db, area_type=None, is_active=None,
        min_latitude=None, min_longitude=None, max_latitude=None, max_longitude=None,
        order_by="created_at", order_dir="desc", limit=100, offset=0
    )
    layers = gis_service.geo_layers.list(
        db=db, layer_type=None, source_type=None, is_active=None,
        order_by="created_at", order_dir="desc", limit=100, offset=0
    )

    # Count coverage areas
    coverage_areas = sum(1 for a in areas if a.area_type == GeoAreaType.coverage or a.area_type == GeoAreaType.service_area)

    context = {
        "request": request,
        "active_page": "gis",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "active_tab": tab,
        "locations": locations,
        "areas": areas,
        "layers": layers,
        "coverage_areas": coverage_areas,
    }
    return templates.TemplateResponse("admin/gis/index.html", context)


@router.get("/locations/new", response_class=HTMLResponse)
def gis_location_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    context = {
        "request": request,
        "active_page": "gis",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "location": None,
        "action_url": "/admin/gis/locations/new",
        "error": None,
    }
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
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        payload = GeoLocationCreate(
            name=name,
            location_type=GeoLocationType(location_type),
            latitude=latitude,
            longitude=longitude,
            notes=notes or None,
            is_active=is_active == "true",
        )
        gis_service.geo_locations.create(db=db, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)
    except Exception as e:
        context = {
            "request": request,
            "active_page": "gis",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "location": None,
            "action_url": "/admin/gis/locations/new",
            "error": str(e),
        }
        return templates.TemplateResponse("admin/gis/location_form.html", context)


@router.get("/locations/{location_id}/edit", response_class=HTMLResponse)
def gis_location_edit(request: Request, location_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    location = gis_service.geo_locations.get(db=db, location_id=location_id)

    context = {
        "request": request,
        "active_page": "gis",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "location": location,
        "action_url": f"/admin/gis/locations/{location_id}/edit",
        "error": None,
    }
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
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        payload = GeoLocationUpdate(
            name=name,
            location_type=GeoLocationType(location_type),
            latitude=latitude,
            longitude=longitude,
            notes=notes or None,
            is_active=is_active == "true",
        )
        gis_service.geo_locations.update(db=db, location_id=location_id, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)
    except Exception as e:
        location = gis_service.geo_locations.get(db=db, location_id=location_id)
        context = {
            "request": request,
            "active_page": "gis",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "location": location,
            "action_url": f"/admin/gis/locations/{location_id}/edit",
            "error": str(e),
        }
        return templates.TemplateResponse("admin/gis/location_form.html", context)
