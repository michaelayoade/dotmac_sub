"""Admin DNS threat monitoring routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_dns_threats as web_network_dns_threats_service
from app.services.auth_dependencies import require_method_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network",
    tags=["web-admin-network"],
    dependencies=[
        Depends(
            require_method_permission(
                "network:dns_threat:read", "network:dns_threat:write"
            )
        )
    ],
)


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/dns-threats", response_class=HTMLResponse)
def dns_threats_list(
    request: Request,
    search: str | None = None,
    severity: str | None = None,
    action: str | None = None,
    subscriber_id: str | None = None,
    network_device_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    data = web_network_dns_threats_service.list_page_data(
        db,
        search=search,
        severity=severity,
        action=action,
        subscriber_id=subscriber_id,
        network_device_id=network_device_id,
    )
    context = _base_context(request, db, active_page="dns-threats")
    context.update(data)
    return templates.TemplateResponse("admin/network/dns_threats/index.html", context)


@router.get("/dns-threats/new", response_class=HTMLResponse)
def dns_threats_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="dns-threats")
    context.update(
        {
            "event": {
                "severity": "medium",
                "action": "blocked",
                "occurred_at": None,
            },
            "action_url": "/admin/network/dns-threats",
            "error": None,
            **web_network_dns_threats_service.event_form_reference_data(db),
        }
    )
    return templates.TemplateResponse("admin/network/dns_threats/form.html", context)


@router.post("/dns-threats", response_class=HTMLResponse)
def dns_threats_create(request: Request, db: Session = Depends(get_db)):
    values = web_network_dns_threats_service.parse_event_form(
        parse_form_data_sync(request)
    )
    result = web_network_dns_threats_service.create_event_from_values(
        db, values, request=request
    )
    if result.error:
        context = _base_context(request, db, active_page="dns-threats")
        context.update(
            {
                "event": result.form_model,
                "action_url": "/admin/network/dns-threats",
                "error": result.error,
                **web_network_dns_threats_service.event_form_reference_data(db),
            }
        )
        return templates.TemplateResponse(
            "admin/network/dns_threats/form.html", context
        )

    return RedirectResponse("/admin/network/dns-threats", status_code=303)
