"""Admin GIS web routes."""

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.gis import GeoAreaType, GeoLayerSource, GeoLayerType, GeoLocationType
from app.schemas.gis import (
    GeoAreaCreate,
    GeoLayerCreate,
    GeoLayerUpdate,
    GeoLocationCreate,
    GeoLocationUpdate,
)
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

    # Build GeoJSON markers for the map
    map_markers = []
    for loc in locations:
        lat = getattr(loc, "latitude", None)
        lon = getattr(loc, "longitude", None)
        if lat is not None and lon is not None:
            loc_type = loc.location_type.value if hasattr(loc.location_type, "value") else str(loc.location_type or "")
            map_markers.append({
                "lat": float(lat),
                "lon": float(lon),
                "name": str(loc.name or ""),
                "type": loc_type,
                "id": str(loc.id),
            })

    area_features = []
    for area in areas:
        if getattr(area, "geometry_geojson", None):
            area_type = area.area_type.value if hasattr(area.area_type, "value") else str(area.area_type or "")
            area_features.append({
                "type": "Feature",
                "geometry": area.geometry_geojson,
                "properties": {
                    "id": str(area.id),
                    "name": str(area.name or ""),
                    "area_type": area_type,
                    "is_active": bool(area.is_active),
                },
            })

    layer_overlays = [
        {
            "id": str(layer.id),
            "name": str(layer.name or ""),
            "layer_key": str(layer.layer_key or ""),
            "layer_type": layer.layer_type.value if hasattr(layer.layer_type, "value") else str(layer.layer_type or ""),
            "source_type": layer.source_type.value if hasattr(layer.source_type, "value") else str(layer.source_type or ""),
            "style": layer.style or {},
            "is_active": bool(layer.is_active),
        }
        for layer in layers
        if layer.is_active and layer.layer_key
    ]

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
        "map_markers": map_markers,
        "area_features": area_features,
        "layer_overlays": layer_overlays,
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


@router.post("/locations/{location_id}/delete", response_class=HTMLResponse)
def gis_location_delete(location_id: str, db: Session = Depends(get_db)):
    gis_service.geo_locations.delete(db=db, location_id=location_id)
    return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)


# ── Area CRUD ────────────────────────────────────────────────────────


def _gis_context(request: Request, db: Session, **extra) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "gis",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **extra,
    }


@router.get("/areas/new", response_class=HTMLResponse)
def gis_area_new(request: Request, db: Session = Depends(get_db)):
    ctx = _gis_context(request, db, area=None, action_url="/admin/gis/areas/new", error=None)
    ctx["area_types"] = [t.value for t in GeoAreaType]
    return templates.TemplateResponse("admin/gis/area_form.html", ctx)


@router.post("/areas/new", response_class=HTMLResponse)
def gis_area_create(
    request: Request,
    name: str = Form(...),
    area_type: str = Form(...),
    geometry_geojson: str = Form(""),
    notes: str = Form(None),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = GeoAreaCreate(
            name=name,
            area_type=GeoAreaType(area_type),
            geometry_geojson=json.loads(geometry_geojson) if geometry_geojson.strip() else None,
            notes=notes or None,
            is_active=is_active == "true",
        )
        gis_service.geo_areas.create(db=db, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=areas", status_code=303)
    except Exception as e:
        ctx = _gis_context(request, db, area=None, action_url="/admin/gis/areas/new", error=str(e))
        ctx["area_types"] = [t.value for t in GeoAreaType]
        return templates.TemplateResponse("admin/gis/area_form.html", ctx)


@router.get("/areas/{area_id}/edit", response_class=HTMLResponse)
def gis_area_edit(request: Request, area_id: str, db: Session = Depends(get_db)):
    area = gis_service.geo_areas.get(db=db, area_id=area_id)
    ctx = _gis_context(request, db, area=area, action_url=f"/admin/gis/areas/{area_id}/edit", error=None)
    ctx["area_types"] = [t.value for t in GeoAreaType]
    return templates.TemplateResponse("admin/gis/area_form.html", ctx)


@router.post("/areas/{area_id}/edit", response_class=HTMLResponse)
def gis_area_update(
    request: Request,
    area_id: str,
    name: str = Form(...),
    area_type: str = Form(...),
    geometry_geojson: str = Form(""),
    notes: str = Form(None),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    try:
        from app.schemas.gis import GeoAreaUpdate

        payload = GeoAreaUpdate(
            name=name,
            area_type=GeoAreaType(area_type),
            geometry_geojson=json.loads(geometry_geojson) if geometry_geojson.strip() else None,
            notes=notes or None,
            is_active=is_active == "true",
        )
        gis_service.geo_areas.update(db=db, area_id=area_id, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=areas", status_code=303)
    except Exception as e:
        area = gis_service.geo_areas.get(db=db, area_id=area_id)
        ctx = _gis_context(request, db, area=area, action_url=f"/admin/gis/areas/{area_id}/edit", error=str(e))
        ctx["area_types"] = [t.value for t in GeoAreaType]
        return templates.TemplateResponse("admin/gis/area_form.html", ctx)


@router.post("/areas/{area_id}/delete", response_class=HTMLResponse)
def gis_area_delete(area_id: str, db: Session = Depends(get_db)):
    gis_service.geo_areas.delete(db=db, area_id=area_id)
    return RedirectResponse(url="/admin/gis?tab=areas", status_code=303)


# ── Layer CRUD ───────────────────────────────────────────────────────


@router.get("/layers/new", response_class=HTMLResponse)
def gis_layer_new(request: Request, db: Session = Depends(get_db)):
    ctx = _gis_context(request, db, layer=None, action_url="/admin/gis/layers/new", error=None)
    ctx["layer_types"] = [t.value for t in GeoLayerType]
    ctx["source_types"] = [t.value for t in GeoLayerSource]
    return templates.TemplateResponse("admin/gis/layer_form.html", ctx)


@router.post("/layers/new", response_class=HTMLResponse)
def gis_layer_create(
    request: Request,
    name: str = Form(...),
    layer_key: str = Form(...),
    layer_type: str = Form(...),
    source_type: str = Form(...),
    style: str = Form("{}"),
    filters: str = Form("{}"),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    import json

    try:
        payload = GeoLayerCreate(
            name=name,
            layer_key=layer_key.strip().lower().replace(" ", "_"),
            layer_type=GeoLayerType(layer_type),
            source_type=GeoLayerSource(source_type),
            style=json.loads(style) if style.strip() else {},
            filters=json.loads(filters) if filters.strip() else {},
            is_active=is_active == "true",
        )
        gis_service.geo_layers.create(db=db, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=layers", status_code=303)
    except Exception as e:
        ctx = _gis_context(request, db, layer=None, action_url="/admin/gis/layers/new", error=str(e))
        ctx["layer_types"] = [t.value for t in GeoLayerType]
        ctx["source_types"] = [t.value for t in GeoLayerSource]
        return templates.TemplateResponse("admin/gis/layer_form.html", ctx)


@router.get("/layers/{layer_id}/edit", response_class=HTMLResponse)
def gis_layer_edit(request: Request, layer_id: str, db: Session = Depends(get_db)):
    layer = gis_service.geo_layers.get(db=db, layer_id=layer_id)
    ctx = _gis_context(
        request,
        db,
        layer=layer,
        action_url=f"/admin/gis/layers/{layer_id}/edit",
        error=None,
    )
    ctx["layer_types"] = [t.value for t in GeoLayerType]
    ctx["source_types"] = [t.value for t in GeoLayerSource]
    return templates.TemplateResponse("admin/gis/layer_form.html", ctx)


@router.post("/layers/{layer_id}/edit", response_class=HTMLResponse)
def gis_layer_update(
    request: Request,
    layer_id: str,
    name: str = Form(...),
    layer_key: str = Form(...),
    layer_type: str = Form(...),
    source_type: str = Form(...),
    style: str = Form("{}"),
    filters: str = Form("{}"),
    is_active: str = Form(None),
    db: Session = Depends(get_db),
):
    import json

    try:
        payload = GeoLayerUpdate(
            name=name,
            layer_key=layer_key.strip().lower().replace(" ", "_"),
            layer_type=GeoLayerType(layer_type),
            source_type=GeoLayerSource(source_type),
            style=json.loads(style) if style.strip() else {},
            filters=json.loads(filters) if filters.strip() else {},
            is_active=is_active == "true",
        )
        gis_service.geo_layers.update(db=db, layer_id=layer_id, payload=payload)
        return RedirectResponse(url="/admin/gis?tab=layers", status_code=303)
    except Exception as e:
        layer = gis_service.geo_layers.get(db=db, layer_id=layer_id)
        ctx = _gis_context(
            request,
            db,
            layer=layer,
            action_url=f"/admin/gis/layers/{layer_id}/edit",
            error=str(e),
        )
        ctx["layer_types"] = [t.value for t in GeoLayerType]
        ctx["source_types"] = [t.value for t in GeoLayerSource]
        return templates.TemplateResponse("admin/gis/layer_form.html", ctx)


@router.post("/layers/{layer_id}/delete", response_class=HTMLResponse)
def gis_layer_delete(layer_id: str, db: Session = Depends(get_db)):
    gis_service.geo_layers.delete(db=db, layer_id=layer_id)
    return RedirectResponse(url="/admin/gis?tab=layers", status_code=303)
