"""Admin network monitoring and alarms web routes."""

import logging
import subprocess
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_alarm_rules as web_network_alarm_rules_service
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services import web_network_monitoring as web_network_monitoring_service
from app.services.audit_helpers import build_audit_activities_for_types
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

_format_duration = web_network_core_runtime_service.format_duration
_format_bps = web_network_core_runtime_service.format_bps


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


# ── VPN tunnel and site reachability helpers ──────────────────────────

_TUNNEL_NAMES = {
    "KX5kLfJ1uMzMHTdLbdMVXTdxgwoDm7FR/xTvTlh2Lyw=": "Abuja Core (Garki)",
    "5EotB4DMlz9h89pRSmmSd2J0krVKRgdJsNzRx1ya5Gw=": "Lagos Medallion",
    "6zaWZIeQkgLRhePeGB+UReEMqbCg+RG95HMTEMQ69Tk=": "Demo NAS (Karu)",
}


def _get_vpn_tunnel_status() -> list[dict]:
    """Read WireGuard peer status from wg show."""
    tunnels = []
    try:
        result = subprocess.run(
            ["wg", "show", "wg0", "dump"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        lines = result.stdout.strip().split("\n")
        for line in lines[1:]:  # skip interface line
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            pubkey = parts[0]
            endpoint = parts[2] if parts[2] != "(none)" else None
            handshake_ts = int(parts[4]) if parts[4] != "0" else 0
            rx_bytes = int(parts[5])
            tx_bytes = int(parts[6])

            handshake_dt = datetime.fromtimestamp(handshake_ts, tz=UTC) if handshake_ts else None
            stale = True
            if handshake_dt:
                stale = (datetime.now(UTC) - handshake_dt) > timedelta(minutes=3)

            tunnels.append({
                "name": _TUNNEL_NAMES.get(pubkey, pubkey[:12] + "..."),
                "endpoint": endpoint,
                "handshake": handshake_dt,
                "handshake_ago": _format_ago(handshake_dt) if handshake_dt else "never",
                "rx": _format_bytes(rx_bytes),
                "tx": _format_bytes(tx_bytes),
                "up": not stale and handshake_ts > 0,
                "stale": stale,
            })
    except FileNotFoundError:
        logger.warning("WireGuard 'wg' command not found — VPN status unavailable")
    except PermissionError:
        logger.warning("Insufficient permissions to read WireGuard status")
    except Exception as exc:
        logger.warning("Failed to read WireGuard status: %s", exc)
    return tunnels


def _get_site_reachability(db: Session) -> list[dict]:
    """Group monitored devices by management subnet and compute reachability."""
    return web_network_monitoring_service.get_site_reachability(db)


def _format_ago(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    if delta.total_seconds() < 60:
        return f"{int(delta.total_seconds())}s ago"
    if delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() / 60)}m ago"
    return f"{int(delta.total_seconds() / 3600)}h ago"


def _format_bytes(b: int) -> str:
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f} GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


@router.get("/monitoring", response_class=HTMLResponse, dependencies=[Depends(require_permission("monitoring:read"))])
def monitoring_page(
    request: Request,
    q: str | None = None,
    refresh: str | None = None,
    db: Session = Depends(get_db),
):
    # Force-refresh dispatches a Celery task instead of blocking the request
    force_refresh = (refresh or "").strip().lower() in {"1", "true", "yes", "on"}
    if force_refresh:
        try:
            from app.celery_app import celery_app as _celery

            _celery.send_task("app.tasks.network_monitoring.refresh_core_device_ping")
            _celery.send_task("app.tasks.network_monitoring.refresh_core_device_snmp")
        except Exception:
            logger.debug("Could not dispatch monitoring refresh task")

    page_data = web_network_monitoring_service.monitoring_page_data(
        db,
        format_duration=_format_duration,
        format_bps=_format_bps,
        query=q,
    )
    context = _base_context(request, db, active_page="monitoring")
    context.update(page_data)
    context["vpn_tunnels"] = _get_vpn_tunnel_status()
    context["site_reachability"] = _get_site_reachability(db)
    context["activities"] = build_audit_activities_for_types(
        db,
        ["core_device", "network_device"],
        limit=5,
    )
    return templates.TemplateResponse("admin/network/monitoring/index.html", context)


@router.get("/monitoring/kpi", response_class=HTMLResponse, dependencies=[Depends(require_permission("monitoring:read"))])
def monitoring_kpi_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: auto-refreshing KPI cards + alarm/outage summary."""
    from app.services.network_monitoring import (
        NetworkDevices,
        get_onu_status_summary,
        get_pon_outage_summary,
    )

    stats = NetworkDevices.get_monitoring_dashboard_stats(
        db, format_duration=_format_duration, format_bps=_format_bps
    )
    onu_summary = get_onu_status_summary(db)
    pon_outages = get_pon_outage_summary(db)
    alarms_data = web_network_monitoring_service.alarms_page_data(db, severity=None, status=None)
    from datetime import UTC, datetime

    # VPN tunnel health from WireGuard
    vpn_tunnels = _get_vpn_tunnel_status()

    # Site reachability summary (group devices by /16 subnet)
    site_reachability = _get_site_reachability(db)

    context = {
        "request": request,
        "stats": stats.get("stats", {}),
        "onu_summary": onu_summary,
        "pon_outages": pon_outages,
        "alarms": alarms_data.get("alarms", []),
        "vpn_tunnels": vpn_tunnels,
        "site_reachability": site_reachability,
        "now": datetime.now(UTC),
    }
    return templates.TemplateResponse("admin/network/monitoring/_kpi_partial.html", context)


@router.get("/alarms", response_class=HTMLResponse, dependencies=[Depends(require_permission("monitoring:read"))])
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


@router.get("/alarms/rules/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("monitoring:read"))])
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
    return templates.TemplateResponse("admin/network/monitoring/rule_form.html", context)


@router.post("/alarms/rules/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("monitoring:write"))])
def alarms_rules_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_alarm_rules_service.parse_form_values(form)
    normalized, error = web_network_alarm_rules_service.validate_form_values(values)
    if not error:
        assert normalized is not None
        error = web_network_alarm_rules_service.create_rule(db, normalized)
        if not error:
            return RedirectResponse(url="/admin/network/alarms", status_code=303)

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
    return templates.TemplateResponse("admin/network/monitoring/rule_form.html", context)


# ── Bulk actions on monitoring devices ────────────────────────────────

_ACTION_LABELS: dict[str, str] = {
    "enable_monitoring": "Enable Monitoring",
    "disable_monitoring": "Disable Monitoring",
    "enable_notifications": "Enable Notifications",
    "disable_notifications": "Disable Notifications",
    "deactivate": "Deactivate",
}


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
    error = stats.get("error")
    if error:
        html = (
            '<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm'
            f' text-rose-700 dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{error}</div>'
        )
    else:
        label = _ACTION_LABELS.get(action, action)
        skipped_text = f", {stats['skipped']} skipped (max 50)" if stats.get("skipped") else ""
        html = (
            '<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm'
            " text-emerald-700 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-400"
            f'">Bulk <strong>{label}</strong>: {stats["succeeded"]} succeeded, {stats["failed"]} failed'
            f"{skipped_text}.</div>"
        )
    return HTMLResponse(html)
