"""Admin UI for configurable customer regions."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_regions
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/customer-regions", tags=["web-admin-customer-regions"])


def _context(request: Request, db: Session, **extra: object) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "settings",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **extra,
    }


def _optional_uuid(value: str | None) -> UUID | None:
    normalized = (value or "").strip()
    return UUID(normalized) if normalized else None


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("gis:map:view"))],
)
def customer_regions_index(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin/customer_regions/index.html",
        _context(
            request,
            db,
            regions=customer_regions.list_regions(db),
            infrastructure_options=customer_regions.infrastructure_options(db),
            match_modes=customer_regions.REGION_MATCH_MODES,
            error=None,
            editing_region=None,
        ),
    )


@router.get(
    "/{region_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("gis:map:view"))],
)
def customer_region_edit(
    region_id: UUID, request: Request, db: Session = Depends(get_db)
):
    region = db.get(customer_regions.CustomerRegion, region_id)
    if region is None:
        return RedirectResponse(url="/admin/customer-regions", status_code=303)
    return templates.TemplateResponse(
        "admin/customer_regions/index.html",
        _context(
            request,
            db,
            regions=customer_regions.list_regions(db),
            infrastructure_options=customer_regions.infrastructure_options(db),
            match_modes=customer_regions.REGION_MATCH_MODES,
            error=None,
            editing_region=region,
        ),
    )


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("gis:area:write"))],
)
def customer_region_save(
    request: Request,
    name: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    radius_meters: float = Form(...),
    color: str = Form(customer_regions.DEFAULT_REGION_COLOR),
    match_mode: str = Form("nearest"),
    priority: int = Form(0),
    nas_device_id: str | None = Form(None),
    pop_site_id: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    region_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        customer_regions.save_region(
            db,
            region_id=_optional_uuid(region_id),
            name=name,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
            color=color,
            match_mode=match_mode,
            priority=priority,
            nas_device_id=_optional_uuid(nas_device_id),
            pop_site_id=_optional_uuid(pop_site_id),
            notes=notes,
            is_active=is_active is not None,
        )
        return RedirectResponse(url="/admin/customer-regions", status_code=303)
    except (ValueError, TypeError) as exc:
        return templates.TemplateResponse(
            "admin/customer_regions/index.html",
            _context(
                request,
                db,
                regions=customer_regions.list_regions(db),
                infrastructure_options=customer_regions.infrastructure_options(db),
                match_modes=customer_regions.REGION_MATCH_MODES,
                error=str(exc),
                editing_region=None,
            ),
            status_code=422,
        )


@router.post(
    "/{region_id}/disable",
    dependencies=[Depends(require_permission("gis:area:write"))],
)
def customer_region_disable(region_id: UUID, db: Session = Depends(get_db)):
    customer_regions.delete_region(db, region_id=region_id)
    return RedirectResponse(url="/admin/customer-regions", status_code=303)
