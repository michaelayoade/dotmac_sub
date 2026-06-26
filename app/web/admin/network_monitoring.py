"""Admin network monitoring and alarms web routes."""

from fastapi import APIRouter, Depends, Form, Query, Request
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
    "/topology-gaps",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def topology_gaps_page(
    request: Request,
    node_page: int = Query(1, ge=1),
    node_per_page: int = Query(50, ge=10, le=200),
    gap_page: int = Query(1, ge=1),
    gap_per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    from app.services.topology.gaps import topology_gaps

    context = _base_context(request, db, active_page="monitoring")
    context["gaps"] = topology_gaps(
        db,
        node_page=node_page,
        node_per_page=node_per_page,
        gap_page=gap_page,
        gap_per_page=gap_per_page,
    )
    return templates.TemplateResponse("admin/network/topology_gaps.html", context)


@router.get(
    "/performance",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def performance_page(
    request: Request,
    tab: str = "wallboard",
    tier: str = "bts",
    window: str = "7d",
    db: Session = Depends(get_db),
):
    """Infrastructure performance & SLA dashboard (BTS / OLT / PON / AP).

    Three tabs: live wallboard, worst-performer ranking, SLA compliance. See
    docs/designs/INFRASTRUCTURE_SLA_PERFORMANCE.md.
    """
    from app.config import settings
    from app.services import web_network_performance as perf

    context = _base_context(request, db, active_page="monitoring")
    active_tab = tab if tab in {"wallboard", "ranking", "sla"} else "wallboard"
    context["active_tab"] = active_tab
    context["tiers"] = perf.TIERS
    context["windows"] = perf.WINDOWS
    context["sel_tier"] = tier if tier in perf.TIERS else "bts"
    context["sel_window"] = window if window in perf.WINDOWS else "7d"
    context["wallboard"] = perf.wallboard(db)
    context["sla_logging_enabled"] = bool(settings.sla_availability_log_enabled)
    if active_tab in {"ranking", "sla"}:
        context["ranking"] = perf.ranking(
            db, context["sel_tier"], context["sel_window"]
        )
    return templates.TemplateResponse("admin/network/performance/index.html", context)


@router.get(
    "/performance/trend",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def performance_trend_page(
    request: Request,
    element_type: str,
    element_id: str,
    name: str = "",
    db: Session = Depends(get_db),
):
    """Availability trend (Chart.js) for one element, from daily snapshots."""
    import uuid as _uuid

    from app.services import infrastructure_availability_snapshot as snap

    context = _base_context(request, db, active_page="monitoring")
    points: list = []
    if element_type in {"device", "pop_site", "pon_port"}:
        try:
            points = snap.trend(db, element_type, _uuid.UUID(element_id), days=365)
        except (ValueError, TypeError):
            points = []
    context["element_name"] = name or element_id
    context["element_type"] = element_type
    context["points"] = points
    return templates.TemplateResponse("admin/network/performance/trend.html", context)


@router.get(
    "/performance/export",
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def performance_export(
    tier: str = "bts",
    window: str = "7d",
    db: Session = Depends(get_db),
):
    from fastapi.responses import Response

    from app.services import web_network_performance as perf

    content = perf.build_ranking_csv(db, tier, window)
    safe_tier = tier if tier in perf.TIERS else "bts"
    safe_window = window if window in perf.WINDOWS else "7d"
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; filename=infra_sla_{safe_tier}_{safe_window}.csv"
            )
        },
    )


@router.get(
    "/outage-impact",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def outage_impact_page(
    request: Request,
    basestation_id: str | None = None,
    node_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Read-only outage impact preview: pick a basestation/node -> affected
    active subscriptions. No incident is created (that's the outage console)."""
    import uuid as _uuid

    from app.models.network_monitoring import NetworkDevice, PopSite
    from app.services.topology.affected import affected_customers, list_basestations

    context = _base_context(request, db, active_page="monitoring")
    context["basestations"] = list_basestations(db)
    context["selected_basestation_id"] = basestation_id
    target = None
    result = None
    try:
        if basestation_id:
            pop = db.get(PopSite, _uuid.UUID(basestation_id))
            if pop is not None:
                target = f"Basestation: {pop.name}"
                result = affected_customers(db, basestation=pop)
        elif node_id:
            node = db.get(NetworkDevice, _uuid.UUID(node_id))
            if node is not None:
                target = f"Node: {node.name}"
                result = affected_customers(db, node=node)
    except (ValueError, TypeError):
        target = None
    context["target"] = target
    if result is not None:
        context["impact_count"] = result["count"]
        context["impact_rows"] = [
            {
                "id": s.id,
                "subscriber": (
                    f"{s.subscriber.first_name} {s.subscriber.last_name}"
                    if s.subscriber
                    else "—"
                ),
                "email": s.subscriber.email if s.subscriber else "",
            }
            for s in result["subscriptions"]
        ]
    return templates.TemplateResponse("admin/network/outage_impact.html", context)


def _actor(request: Request) -> str | None:
    from app.web.admin import get_current_user

    user = get_current_user(request)
    if isinstance(user, dict):
        return user.get("email") or user.get("username")
    return getattr(user, "email", None) or getattr(user, "username", None)


@router.get(
    "/outages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def outages_console(request: Request, db: Session = Depends(get_db)):
    """Manual outage console: declare against a basestation, list/resolve open
    incidents. No auto-detection, no notification sending."""
    from app.models.network_monitoring import NetworkDevice, PopSite
    from app.services.topology.affected import list_basestations
    from app.services.topology.outage import is_stale_open, list_open_incidents

    context = _base_context(request, db, active_page="monitoring")
    context["basestations"] = list_basestations(db)
    rows = []
    for inc in list_open_incidents(db):
        if inc.basestation_id is not None:
            pop = db.get(PopSite, inc.basestation_id)
            target = f"BTS: {pop.name}" if pop else "BTS"
        elif inc.root_node_id is not None:
            node = db.get(NetworkDevice, inc.root_node_id)
            target = f"Node: {node.name}" if node else "Node"
        else:
            target = "—"
        rows.append({"incident": inc, "target": target, "stale": is_stale_open(inc)})
    context["incidents"] = rows
    return templates.TemplateResponse("admin/network/outages.html", context)


@router.post(
    "/outages/declare",
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def outages_declare(
    request: Request,
    basestation_id: str = Form(...),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
):
    import uuid as _uuid

    from app.models.network_monitoring import PopSite
    from app.services.topology.outage import declare_outage

    try:
        pop = db.get(PopSite, _uuid.UUID(basestation_id))
    except (ValueError, TypeError):
        pop = None
    if pop is not None:
        declare_outage(db, basestation=pop, declared_by=_actor(request), note=note)
        db.commit()
    return RedirectResponse("/admin/network/outages", status_code=303)


@router.post(
    "/outages/{incident_id}/resolve",
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def outages_resolve(incident_id: str, request: Request, db: Session = Depends(get_db)):
    import uuid as _uuid

    from app.services.topology.outage import resolve_outage

    try:
        resolve_outage(db, _uuid.UUID(incident_id))
        db.commit()
    except (ValueError, TypeError):
        pass
    return RedirectResponse("/admin/network/outages", status_code=303)


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
    "/monitoring/bandwidth",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def monitoring_bandwidth_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: auto-refreshing live network bandwidth + per-NAS throughput."""
    context = {
        "request": request,
        **web_network_monitoring_service.monitoring_bandwidth_context(db),
    }
    return templates.TemplateResponse(
        "admin/network/monitoring/_bandwidth_partial.html", context
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
