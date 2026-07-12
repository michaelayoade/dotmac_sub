"""Admin UISP desired/observed control-plane views."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.uisp_control import UispDeviceIntent, UispIntentStatus
from app.services.auth_dependencies import require_permission
from app.services.uisp_control_plane import capabilities, request_apply

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


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def uisp_control_list(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = db.query(UispDeviceIntent)
    if status:
        try:
            selected_status = UispIntentStatus(status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid UISP status") from exc
        query = query.filter(UispDeviceIntent.status == selected_status)
    intents = query.order_by(UispDeviceIntent.updated_at.desc()).limit(500).all()
    counts: dict[str, int] = {}
    for intent in db.query(UispDeviceIntent.status).all():
        value = intent[0].value
        counts[value] = counts.get(value, 0) + 1
    context = _context(request, db)
    context.update(
        {
            "intents": intents,
            "counts": counts,
            "capabilities": capabilities(),
            "status_filter": status,
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
    intent = (
        db.query(UispDeviceIntent)
        .options(selectinload(UispDeviceIntent.snapshots))
        .filter(UispDeviceIntent.id == intent_id)
        .one_or_none()
    )
    if intent is None:
        raise HTTPException(status_code=404, detail="UISP intent not found")
    context = _context(request, db)
    from app.services.uisp_control_plane import redact_config

    context.update(
        {
            "intent": intent,
            "desired_redacted": redact_config(intent.desired_config),
            "capabilities": capabilities(),
        }
    )
    return templates.TemplateResponse("admin/network/uisp-control/detail.html", context)


@router.post(
    "/{intent_id}/apply",
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def uisp_control_apply(intent_id: UUID, db: Session = Depends(get_db)):
    intent = db.get(UispDeviceIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="UISP intent not found")
    request_apply(db, intent)
    return RedirectResponse(
        f"/admin/network/uisp-control/{intent_id}?notice=manual_required",
        status_code=303,
    )


@router.post(
    "/{intent_id}/desired",
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def uisp_control_update_desired(
    intent_id: UUID,
    name: str = Form(""),
    management_ip: str = Form(""),
    firmware_version: str = Form(""),
    wifi_ssid: str = Form(""),
    wifi_password: str = Form(""),
    remote_access_enabled: bool = Form(False),
    lifecycle_state: str = Form("active"),
    db: Session = Depends(get_db),
):
    from app.services.uisp_control_plane import UispIntentError, update_intent_desired

    intent = db.get(UispDeviceIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="UISP intent not found")
    try:
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
    except UispIntentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/admin/network/uisp-control/{intent_id}?notice=staged",
        status_code=303,
    )
