"""Admin network monitoring and alarms web routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_alarm_rules as web_network_alarm_rules_service
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services import web_network_monitoring as web_network_monitoring_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

_format_duration = web_network_core_runtime_service.format_duration
_format_bps = web_network_core_runtime_service.format_bps


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


@router.get(
    "/monitoring",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def monitoring_page(
    request: Request,
    q: str | None = None,
    refresh: str | None = None,
    db: Session = Depends(get_db),
):
    force_refresh = (refresh or "").strip().lower() in {"1", "true", "yes", "on"}
    if force_refresh:
        web_network_monitoring_service.dispatch_monitoring_refresh(
            request_id=getattr(request.state, "request_id", None)
        )

    page_data = web_network_monitoring_service.monitoring_index_context(
        db,
        format_duration=_format_duration,
        format_bps=_format_bps,
        query=q,
    )
    context = _base_context(request, db, active_page="monitoring")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/monitoring/index.html", context)


@router.get(
    "/monitoring/kpi",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def monitoring_kpi_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: auto-refreshing KPI cards + alarm/outage summary."""
    context = {
        "request": request,
        **web_network_monitoring_service.monitoring_kpi_context(
            db,
            format_duration=_format_duration,
            format_bps=_format_bps,
        ),
    }
    return templates.TemplateResponse(
        "admin/network/monitoring/_kpi_partial.html", context
    )


@router.get(
    "/alarms",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def alarms_page(
    request: Request,
    severity: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    page_data = web_network_monitoring_service.alarms_page_data(
        db,
        severity=severity,
        status=status,
    )
    context = _base_context(request, db, active_page="monitoring")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/monitoring/alarms.html", context)


@router.get(
    "/alarms/rules/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def alarms_rules_new(request: Request, db: Session = Depends(get_db)):
    options = web_network_alarm_rules_service.form_options(db)
    context = _base_context(request, db, active_page="monitoring")
    context.update(
        {
            "rule": None,
            "action_url": "/admin/network/alarms/rules/new",
            **options,
        }
    )
    return templates.TemplateResponse(
        "admin/network/monitoring/rule_form.html", context
    )


@router.post(
    "/alarms/rules/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def alarms_rules_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_alarm_rules_service.parse_form_values(form)
    normalized, error = web_network_alarm_rules_service.validate_form_values(values)
    if not error and normalized is not None:
        error = web_network_alarm_rules_service.create_rule(db, normalized)
        if not error:
            return RedirectResponse(url="/admin/network/alarms", status_code=303)
    elif not error:
        error = "Please correct the highlighted fields."

    options = web_network_alarm_rules_service.form_options(db)
    rule = web_network_alarm_rules_service.rule_form_data(values)
    context = _base_context(request, db, active_page="monitoring")
    context.update(
        {
            "rule": rule,
            "action_url": "/admin/network/alarms/rules/new",
            **options,
            "error": error or "Please correct the highlighted fields.",
        }
    )
    return templates.TemplateResponse(
        "admin/network/monitoring/rule_form.html", context
    )


@router.post(
    "/monitoring/bulk-action",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def monitoring_device_bulk_action(
    request: Request,
    action: str = Form(""),
    device_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute a bulk action on selected monitoring devices."""
    stats = web_network_monitoring_service.execute_device_bulk_action(
        db, device_ids, action
    )
    return HTMLResponse(
        web_network_monitoring_service.render_bulk_result(stats, action)
    )
