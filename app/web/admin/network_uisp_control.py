"""Admin UISP desired/observed control-plane views."""

from __future__ import annotations

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.uisp_control import UispDeviceIntent, UispIntentStatus
from app.services import uisp_control_plane
from app.services.auth_dependencies import require_permission
from app.services.uisp_control_plane import capabilities, request_apply
from app.services.uisp_write_adapter import (
    UispWriteUnsupported,
    capability_profile,
)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network/uisp-control", tags=["web-admin-uisp"])


def _context(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "uisp-control",
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _detail_redirect(
    intent_id: UUID, *, notice: str | None = None, error: str | None = None
) -> RedirectResponse:
    params = []
    if notice:
        params.append(f"notice={quote_plus(notice)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(
        f"/admin/network/uisp-control/{intent_id}{suffix}", status_code=303
    )


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def uisp_control_list(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    selected_status = None
    if status:
        try:
            selected_status = UispIntentStatus(status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid UISP status") from exc
    total = uisp_control_plane.count_intents(db, status=selected_status)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    intents = uisp_control_plane.list_intents(
        db,
        status=selected_status,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    counts = uisp_control_plane.intent_status_counts(db)
    context = _context(request, db)
    context.update(
        {
            "intents": intents,
            "counts": counts,
            "capabilities": capabilities(),
            "status_filter": status,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
            },
        }
    )
    return templates.TemplateResponse("admin/network/uisp-control/index.html", context)


@router.get(
    "/{intent_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def uisp_control_detail(
    request: Request, intent_id: UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    intent = uisp_control_plane.get_intent_with_snapshots(db, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="UISP intent not found")
    context = _context(request, db)
    from app.services.uisp_control_plane import redact_config

    profile = None
    capability_error = None
    try:
        profile = capability_profile(db, intent)
    except UispWriteUnsupported as exc:
        capability_error = str(exc)

    context.update(
        {
            "intent": intent,
            "desired_redacted": redact_config(intent.desired_state),
            "capabilities": capabilities(),
            "capability_profile": profile,
            "capability_error": capability_error,
        }
    )
    return templates.TemplateResponse("admin/network/uisp-control/detail.html", context)


@router.post(
    "/{intent_id}/apply",
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def uisp_control_apply(intent_id: UUID, db: Session = Depends(get_db)):
    from app.services.uisp_control_plane import UispIntentError

    intent = db.get(UispDeviceIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="UISP intent not found")
    try:
        request_apply(db, intent)
    except UispIntentError as exc:
        return _detail_redirect(intent_id, error=str(exc))
    return _detail_redirect(intent_id, notice="queued")


@router.post(
    "/{intent_id}/desired",
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def uisp_control_update_desired(
    intent_id: UUID,
    name: str | None = Form(None),
    management_ip: str | None = Form(None),
    firmware_version: str | None = Form(None),
    wifi_ssid: str | None = Form(None),
    wifi_password: str | None = Form(None),
    remote_access_enabled: bool | None = Form(None),
    lifecycle_state: str | None = Form(None),
    db: Session = Depends(get_db),
):
    from app.services.uisp_control_plane import UispIntentError, update_intent_desired

    intent = db.get(UispDeviceIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="UISP intent not found")
    try:
        profile = capability_profile(db, intent)
        submitted_fields = {
            field
            for field, value in (
                ("name", name),
                ("management_ip", management_ip),
                ("firmware_version", firmware_version),
                ("wifi.ssid", wifi_ssid),
                ("wifi.password_ref", wifi_password),
                ("remote_access.enabled", remote_access_enabled),
                ("lifecycle.state", lifecycle_state),
            )
            if value is not None
        }
        unsupported = sorted(submitted_fields - set(profile.writable_fields))
        if unsupported:
            raise UispIntentError(
                "Fields are not mapped for this UISP model: " + ", ".join(unsupported)
            )
        update_intent_desired(
            db,
            intent,
            name=name,
            management_ip=management_ip,
            firmware_version=firmware_version,
            wifi_ssid=wifi_ssid or None,
            wifi_password=wifi_password or None,
            remote_access_enabled=remote_access_enabled,
            lifecycle_state=lifecycle_state,
        )
    except (UispIntentError, UispWriteUnsupported) as exc:
        return _detail_redirect(intent_id, error=str(exc))
    return _detail_redirect(intent_id, notice="staged")


@router.post(
    "/{intent_id}/desired/prune",
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def uisp_control_prune_desired(
    intent_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    from app.services.uisp_control_plane import prune_unsupported_desired

    intent = db.get(UispDeviceIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="UISP intent not found")
    try:
        prune_unsupported_desired(db, intent)
    except UispWriteUnsupported as exc:
        return _detail_redirect(intent_id, error=str(exc))
    return _detail_redirect(intent_id, notice="pruned")
