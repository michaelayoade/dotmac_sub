"""Admin routes for OLT and ONT firmware artifact management."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_firmware_catalog as web_service
from app.services.auth_dependencies import require_permission
from app.services.network import firmware_catalog
from app.web.request_parsing import parse_form_data_sync
from app.web.templates import templates

router = APIRouter(
    prefix="/network/firmware-images", tags=["web-admin-network-firmware"]
)


def _redirect(*, feedback: str | None = None, error: str | None = None):
    params = []
    if feedback:
        params.append(f"feedback={quote_plus(feedback)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/admin/network/firmware-images{suffix}", status_code=303)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:vendor_capability:read"))],
)
def firmware_images_list(
    request: Request,
    kind: str = "all",
    search: str | None = None,
    vendor: str | None = None,
    status: str = "all",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    feedback: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_service.list_context(
        request,
        db,
        kind=kind,
        search=search,
        vendor=vendor,
        status=status,
        page=page,
        per_page=per_page,
        feedback=feedback,
        error=error,
    )
    return templates.TemplateResponse("admin/network/firmware/index.html", context)


@router.get(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def firmware_image_create_form(
    request: Request, kind: str = "ont", db: Session = Depends(get_db)
) -> HTMLResponse:
    try:
        firmware_catalog.model_for_kind(kind)
        context = web_service.form_context(request, db, kind=kind)
    except firmware_catalog.FirmwareCatalogError as exc:
        return _redirect(error=str(exc))
    return templates.TemplateResponse("admin/network/firmware/form.html", context)


@router.post(
    "/create",
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def firmware_image_create(request: Request, db: Session = Depends(get_db)) -> Response:
    values = web_service.parse_form(parse_form_data_sync(request))
    try:
        firmware_catalog.create_image(db, values)
    except firmware_catalog.FirmwareCatalogError as exc:
        context = web_service.form_context(
            request,
            db,
            kind=str(values.get("kind") or "ont"),
            values=values,
            error=str(exc),
        )
        return templates.TemplateResponse("admin/network/firmware/form.html", context)
    return _redirect(feedback="Firmware image created.")


@router.get(
    "/{kind}/{image_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def firmware_image_edit_form(
    request: Request, kind: str, image_id: str, db: Session = Depends(get_db)
) -> Response:
    try:
        context = web_service.form_context(request, db, kind=kind, image_id=image_id)
    except firmware_catalog.FirmwareCatalogError as exc:
        return _redirect(error=str(exc))
    return templates.TemplateResponse("admin/network/firmware/form.html", context)


@router.post(
    "/{kind}/{image_id}/edit",
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def firmware_image_update(
    request: Request, kind: str, image_id: str, db: Session = Depends(get_db)
) -> Response:
    values = web_service.parse_form(parse_form_data_sync(request))
    try:
        firmware_catalog.update_image(db, kind, image_id, values)
    except firmware_catalog.FirmwareCatalogError as exc:
        try:
            context = web_service.form_context(
                request,
                db,
                kind=kind,
                image_id=image_id,
                values=values,
                error=str(exc),
            )
        except firmware_catalog.FirmwareCatalogError:
            return _redirect(error=str(exc))
        return templates.TemplateResponse("admin/network/firmware/form.html", context)
    return _redirect(feedback="Firmware image updated.")


@router.post(
    "/{kind}/{image_id}/deactivate",
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def firmware_image_deactivate(
    kind: str, image_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    try:
        firmware_catalog.deactivate_image(db, kind, image_id)
    except firmware_catalog.FirmwareCatalogError as exc:
        return _redirect(error=str(exc))
    return _redirect(feedback="Firmware image deactivated.")
