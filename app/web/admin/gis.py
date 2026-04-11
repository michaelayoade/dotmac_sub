"""Admin GIS web routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_gis as web_gis_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/gis", tags=["web-admin-gis"])


def _gis_context(request: Request, db: Session, **extra) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "gis",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **extra,
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def gis_index(request: Request, tab: str = "locations", db: Session = Depends(get_db)):
    context = _gis_context(
        request,
        db,
        **web_gis_service.build_index_data(db, tab=tab),
    )
    return templates.TemplateResponse("admin/gis/index.html", context)


@router.get("/locations/new", response_class=HTMLResponse)
def gis_location_new(request: Request, db: Session = Depends(get_db)):
    context = _gis_context(
        request,
        db,
        **web_gis_service.location_form_data(
            location=None,
            action_url="/admin/gis/locations/new",
        ),
    )
    return templates.TemplateResponse("admin/gis/location_form.html", context)


@router.post(
    "/locations/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
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
        web_gis_service.create_location_from_form(
            db,
            name=name,
            location_type=location_type,
            latitude=latitude,
            longitude=longitude,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)
    except Exception as e:
        context = _gis_context(
            request,
            db,
            **web_gis_service.location_form_data(
                location=None,
                action_url="/admin/gis/locations/new",
                error=str(e),
            ),
        )
        return templates.TemplateResponse("admin/gis/location_form.html", context)


@router.get("/locations/{location_id}/edit", response_class=HTMLResponse)
def gis_location_edit(
    request: Request, location_id: str, db: Session = Depends(get_db)
):
    location = web_gis_service.get_location(db, location_id=location_id)
    context = _gis_context(
        request,
        db,
        **web_gis_service.location_form_data(
            location=location,
            action_url=f"/admin/gis/locations/{location_id}/edit",
        ),
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
        web_gis_service.update_location_from_form(
            db,
            location_id=location_id,
            name=name,
            location_type=location_type,
            latitude=latitude,
            longitude=longitude,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)
    except Exception as e:
        location = web_gis_service.get_location(db, location_id=location_id)
        context = _gis_context(
            request,
            db,
            **web_gis_service.location_form_data(
                location=location,
                action_url=f"/admin/gis/locations/{location_id}/edit",
                error=str(e),
            ),
        )
        return templates.TemplateResponse("admin/gis/location_form.html", context)


@router.post("/locations/{location_id}/delete", response_class=HTMLResponse)
def gis_location_delete(location_id: str, db: Session = Depends(get_db)):
    web_gis_service.delete_location(db, location_id=location_id)
    return RedirectResponse(url="/admin/gis?tab=locations", status_code=303)


# ── Area CRUD ────────────────────────────────────────────────────────


@router.get("/areas/new", response_class=HTMLResponse)
def gis_area_new(request: Request, db: Session = Depends(get_db)):
    ctx = _gis_context(
        request,
        db,
        **web_gis_service.area_form_data(
            area=None,
            action_url="/admin/gis/areas/new",
        ),
    )
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
        web_gis_service.create_area_from_form(
            db,
            name=name,
            area_type=area_type,
            geometry_geojson=geometry_geojson,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/gis?tab=areas", status_code=303)
    except Exception as e:
        ctx = _gis_context(
            request,
            db,
            **web_gis_service.area_form_data(
                area=None,
                action_url="/admin/gis/areas/new",
                error=str(e),
            ),
        )
        return templates.TemplateResponse("admin/gis/area_form.html", ctx)


@router.get("/areas/{area_id}/edit", response_class=HTMLResponse)
def gis_area_edit(request: Request, area_id: str, db: Session = Depends(get_db)):
    area = web_gis_service.get_area(db, area_id=area_id)
    ctx = _gis_context(
        request,
        db,
        **web_gis_service.area_form_data(
            area=area,
            action_url=f"/admin/gis/areas/{area_id}/edit",
        ),
    )
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
        web_gis_service.update_area_from_form(
            db,
            area_id=area_id,
            name=name,
            area_type=area_type,
            geometry_geojson=geometry_geojson,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/gis?tab=areas", status_code=303)
    except Exception as e:
        area = web_gis_service.get_area(db, area_id=area_id)
        ctx = _gis_context(
            request,
            db,
            **web_gis_service.area_form_data(
                area=area,
                action_url=f"/admin/gis/areas/{area_id}/edit",
                error=str(e),
            ),
        )
        return templates.TemplateResponse("admin/gis/area_form.html", ctx)


@router.post("/areas/{area_id}/delete", response_class=HTMLResponse)
def gis_area_delete(area_id: str, db: Session = Depends(get_db)):
    web_gis_service.delete_area(db, area_id=area_id)
    return RedirectResponse(url="/admin/gis?tab=areas", status_code=303)


# ── Layer CRUD ───────────────────────────────────────────────────────


@router.get("/layers/new", response_class=HTMLResponse)
def gis_layer_new(request: Request, db: Session = Depends(get_db)):
    ctx = _gis_context(
        request,
        db,
        **web_gis_service.layer_form_data(
            layer=None,
            action_url="/admin/gis/layers/new",
        ),
    )
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
    try:
        web_gis_service.create_layer_from_form(
            db,
            name=name,
            layer_key=layer_key,
            layer_type=layer_type,
            source_type=source_type,
            style=style,
            filters=filters,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/gis?tab=layers", status_code=303)
    except Exception as e:
        ctx = _gis_context(
            request,
            db,
            **web_gis_service.layer_form_data(
                layer=None,
                action_url="/admin/gis/layers/new",
                error=str(e),
            ),
        )
        return templates.TemplateResponse("admin/gis/layer_form.html", ctx)


@router.get("/layers/{layer_id}/edit", response_class=HTMLResponse)
def gis_layer_edit(request: Request, layer_id: str, db: Session = Depends(get_db)):
    layer = web_gis_service.get_layer(db, layer_id=layer_id)
    ctx = _gis_context(
        request,
        db,
        **web_gis_service.layer_form_data(
            layer=layer,
            action_url=f"/admin/gis/layers/{layer_id}/edit",
        ),
    )
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
    try:
        web_gis_service.update_layer_from_form(
            db,
            layer_id=layer_id,
            name=name,
            layer_key=layer_key,
            layer_type=layer_type,
            source_type=source_type,
            style=style,
            filters=filters,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/gis?tab=layers", status_code=303)
    except Exception as e:
        layer = web_gis_service.get_layer(db, layer_id=layer_id)
        ctx = _gis_context(
            request,
            db,
            **web_gis_service.layer_form_data(
                layer=layer,
                action_url=f"/admin/gis/layers/{layer_id}/edit",
                error=str(e),
            ),
        )
        return templates.TemplateResponse("admin/gis/layer_form.html", ctx)


@router.post("/layers/{layer_id}/delete", response_class=HTMLResponse)
def gis_layer_delete(layer_id: str, db: Session = Depends(get_db)):
    web_gis_service.delete_layer(db, layer_id=layer_id)
    return RedirectResponse(url="/admin/gis?tab=layers", status_code=303)
