"""Admin UI for configurable customer regions."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_regions
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

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


def _safe_optional_uuid(value: str | None) -> UUID | None:
    try:
        return _optional_uuid(value)
    except (TypeError, ValueError):
        return None


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
    region = customer_regions.get_region(db, region_id=region_id)
    if region is None:
        return RedirectResponse(url="/admin/customer-regions", status_code=303)
    return templates.TemplateResponse(
        "admin/customer_regions/index.html",
        _context(
            request,
            db,
            regions=customer_regions.list_regions(db),
            infrastructure_options=customer_regions.infrastructure_options(
                db,
                nas_device_id=region.nas_device_id,
                pop_site_id=region.pop_site_id,
            ),
            match_modes=customer_regions.REGION_MATCH_MODES,
            error=None,
            editing_region=region,
        ),
    )


@router.post(
    "",
    response_class=HTMLResponse,
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
    auth: dict[str, object] = Depends(require_permission(customer_regions.WRITE_SCOPE)),
):
    submitted_region = None
    form_values = {
        "region_id": region_id or "",
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "radius_meters": radius_meters,
        "color": color,
        "match_mode": match_mode,
        "priority": priority,
        "nas_device_id": nas_device_id or "",
        "pop_site_id": pop_site_id or "",
        "notes": notes or "",
        "is_active": is_active is not None,
    }
    try:
        parsed_region_id = _optional_uuid(region_id)
        command_id = uuid4()
        db_session_adapter.release_read_transaction(db)
        customer_regions.save_region(
            db,
            customer_regions.SaveCustomerRegionCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=(
                        f"{auth.get('principal_type') or 'user'}:"
                        f"{auth.get('principal_id') or 'unknown'}"
                    ),
                    scope=customer_regions.WRITE_SCOPE,
                    reason=(
                        "Network operator updated customer region"
                        if parsed_region_id
                        else "Network operator created customer region"
                    ),
                    idempotency_key=f"customer-region:save:{command_id}",
                ),
                region_id=parsed_region_id,
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
            ),
        )
        return RedirectResponse(url="/admin/customer-regions", status_code=303)
    except (DomainError, ValueError, TypeError) as exc:
        error = exc.message if isinstance(exc, DomainError) else str(exc)
        try:
            parsed_region_id = _optional_uuid(region_id)
        except (TypeError, ValueError):
            parsed_region_id = None
        if parsed_region_id is not None:
            submitted_region = customer_regions.get_region(
                db, region_id=parsed_region_id
            )
        return templates.TemplateResponse(
            "admin/customer_regions/index.html",
            _context(
                request,
                db,
                regions=customer_regions.list_regions(db),
                infrastructure_options=customer_regions.infrastructure_options(
                    db,
                    nas_device_id=_safe_optional_uuid(nas_device_id),
                    pop_site_id=_safe_optional_uuid(pop_site_id),
                ),
                match_modes=customer_regions.REGION_MATCH_MODES,
                error=error,
                editing_region=submitted_region,
                form_values=form_values,
            ),
            status_code=422,
        )


@router.post(
    "/{region_id}/disable",
)
def customer_region_disable(
    region_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(require_permission(customer_regions.WRITE_SCOPE)),
):
    try:
        command_id = uuid4()
        db_session_adapter.release_read_transaction(db)
        customer_regions.delete_region(
            db,
            customer_regions.DisableCustomerRegionCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=(
                        f"{auth.get('principal_type') or 'user'}:"
                        f"{auth.get('principal_id') or 'unknown'}"
                    ),
                    scope=customer_regions.WRITE_SCOPE,
                    reason="Network operator disabled customer region",
                    idempotency_key=f"customer-region:disable:{region_id}",
                ),
                region_id=region_id,
            ),
        )
    except (DomainError, ValueError, TypeError) as exc:
        db_session_adapter.discard_failed_transaction(db)
        error = exc.message if isinstance(exc, DomainError) else str(exc)
        return templates.TemplateResponse(
            "admin/customer_regions/index.html",
            _context(
                request,
                db,
                regions=customer_regions.list_regions(db),
                infrastructure_options=customer_regions.infrastructure_options(db),
                match_modes=customer_regions.REGION_MATCH_MODES,
                error=error,
                editing_region=None,
            ),
            status_code=422,
        )
    return RedirectResponse(url="/admin/customer-regions", status_code=303)
