"""Service helpers for admin dashboard routes."""

import logging
from datetime import UTC, datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntUnit
from app.models.network_monitoring import (
    DeviceInterface,
    InterfaceStatus,
    NetworkDevice,
)
from app.models.subscriber import Subscriber
from app.services import (
    audit as audit_service,
)
from app.services import (
    settings_spec,
)
from app.services import (
    subscriber as subscriber_service,
)
from app.services import (
    system_health as system_health_service,
)
from app.services import (
    web_admin as web_admin_service,
)
from app.services.audit_helpers import (
    extract_changes,
    format_audit_datetime,
    format_changes,
    humanize_action,
    humanize_entity,
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")


def _invoice_total(inv) -> float:
    return float(getattr(inv, "total", None) or getattr(inv, "total_amount", 0) or 0)


def _float_setting(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_user_actor(actor_type) -> bool:
    return actor_type in {AuditActorType.user, AuditActorType.user.value, "user"}


def _build_pon_interface_summary(db: Session) -> dict[str, int]:
    """Return dashboard-friendly counts for PON-related monitoring interfaces.

    Uses SQL-level filtering and aggregation for performance.
    """
    # SQL ILIKE patterns for PON interfaces
    pon_pattern = or_(
        func.lower(func.coalesce(DeviceInterface.name, "")).like("%pon%"),
        func.lower(func.coalesce(DeviceInterface.name, "")).like("%gpon%"),
        func.lower(func.coalesce(DeviceInterface.name, "")).like("%epon%"),
        func.lower(func.coalesce(DeviceInterface.name, "")).like("%xgpon%"),
        func.lower(func.coalesce(DeviceInterface.name, "")).like("%xgs%"),
        func.lower(func.coalesce(DeviceInterface.description, "")).like("%pon%"),
        func.lower(func.coalesce(DeviceInterface.description, "")).like("%gpon%"),
        func.lower(func.coalesce(DeviceInterface.description, "")).like("%epon%"),
        func.lower(func.coalesce(DeviceInterface.description, "")).like("%xgpon%"),
        func.lower(func.coalesce(DeviceInterface.description, "")).like("%xgs%"),
    )

    counts = (
        db.query(
            func.count(DeviceInterface.id).label("total"),
            func.count(DeviceInterface.id)
            .filter(DeviceInterface.status == InterfaceStatus.up)
            .label("up"),
            func.count(DeviceInterface.id)
            .filter(DeviceInterface.status == InterfaceStatus.down)
            .label("down"),
        )
        .join(NetworkDevice, NetworkDevice.id == DeviceInterface.device_id)
        .filter(NetworkDevice.is_active.is_(True))
        .filter(pon_pattern)
        .one()
    )

    total = counts.total or 0
    up = counts.up or 0
    down = counts.down or 0
    unknown = total - up - down

    return {"up": up, "down": down, "unknown": unknown, "total": total}


def _build_health_thresholds(db: Session) -> dict:
    """Resolve network/server health thresholds from settings."""
    return {
        "disk_warn_pct": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "server_health_disk_warn_pct"
            )
        ),
        "disk_crit_pct": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "server_health_disk_crit_pct"
            )
        ),
        "mem_warn_pct": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "server_health_mem_warn_pct"
            )
        ),
        "mem_crit_pct": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "server_health_mem_crit_pct"
            )
        ),
        "load_warn": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "server_health_load_warn"
            )
        ),
        "load_crit": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "server_health_load_crit"
            )
        ),
        "network_warn_pct": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "network_health_warn_pct"
            )
        ),
        "network_crit_pct": _float_setting(
            settings_spec.resolve_value(
                db, SettingDomain.network_monitoring, "network_health_crit_pct"
            )
        ),
    }


def _build_recent_activities(
    recent_activity: list, subscribers_lookup: dict
) -> list[dict]:
    """Transform audit events into display-ready activity dicts."""
    recent_activities = []
    for event in recent_activity[:5]:
        activity_type = "info"
        action = getattr(event, "action", "")
        entity_type = getattr(event, "entity_type", "")
        entity_id = getattr(event, "entity_id", None)

        if "payment" in action.lower() or "invoice" in entity_type.lower():
            activity_type = "payment"
        elif "subscriber" in entity_type.lower():
            activity_type = "signup" if "create" in action.lower() else "activation"

        actor_name = None
        if event.actor_id and _is_user_actor(getattr(event, "actor_type", None)):
            actor = subscribers_lookup.get(str(event.actor_id))
            if actor:
                actor_name = f"{actor.first_name} {actor.last_name}".strip()
        if not actor_name:
            metadata = getattr(event, "metadata_", None) or {}
            actor_name = (
                metadata.get("actor_name") or metadata.get("actor_email") or "System"
            )

        time_str = format_audit_datetime(getattr(event, "occurred_at", None), "%H:%M")

        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(action)
        entity_label = humanize_entity(entity_type, entity_id)

        message = f"{actor_name} {action_label} {entity_label}"
        detail = change_summary or entity_label

        recent_activities.append(
            {
                "type": activity_type,
                "message": message,
                "detail": detail,
                "time": time_str,
            }
        )
    return recent_activities


def dashboard(request: Request, db: Session):
    """Build the main admin dashboard context and return TemplateResponse."""
    from app.services import network_monitoring as network_monitoring_service
    from app.services.billing.reporting import billing_reporting

    # --- Server health ---
    server_health = system_health_service.get_system_health()
    thresholds = _build_health_thresholds(db)
    server_health_status = system_health_service.evaluate_health(
        server_health, thresholds
    )

    # --- Centralized stats ---
    sub_stats = subscriber_service.subscribers.get_dashboard_stats(db)
    net_stats = network_monitoring_service.network_devices.get_dashboard_stats(db)
    billing_stats = billing_reporting.get_dashboard_stats(db)

    # --- OLT/ONT inventory counts (kept for network health ring) ---
    olt_total = db.query(func.count(OLTDevice.id)).scalar() or 0
    olt_online = (
        db.query(func.count(OLTDevice.id))
        .filter(OLTDevice.is_active.is_(True))
        .scalar()
        or 0
    )
    ont_total = db.query(func.count(OntUnit.id)).scalar() or 0
    ont_active = (
        db.query(func.count(OntUnit.id)).filter(OntUnit.is_active.is_(True)).scalar()
        or 0
    )
    # Fall back to monitoring devices if no OLTs are defined
    if olt_total == 0 and net_stats["total_count"] > 0:
        olts_total = net_stats["total_count"]
        olts_online = (
            net_stats["online_count"]
            + net_stats["degraded_count"]
            + net_stats["maintenance_count"]
        )
    else:
        olts_total = olt_total
        olts_online = olt_online

    # --- Network health status ---
    health_pct = int((olts_online / olts_total) * 100) if olts_total > 0 else 0
    warn_pct = thresholds.get("network_warn_pct") or 90
    crit_pct = thresholds.get("network_crit_pct") or 70
    if health_pct >= warn_pct:
        health_status = "healthy"
    elif health_pct >= crit_pct:
        health_status = "warning"
    else:
        health_status = "critical"

    # --- Billing summary ---
    b_stats = billing_stats.get("stats", {})
    payments_this_month = b_stats.get("payments_amount", 0)
    pending_amount = b_stats.get("pending_amount", 0)
    overdue_amount = b_stats.get("overdue_amount", 0)
    active_subscribers = sub_stats["active_count"]
    arpu = payments_this_month / active_subscribers if active_subscribers > 0 else 0

    # --- AR aging breakdown ---
    ar_30 = 0.0
    ar_60 = 0.0
    try:
        from sqlalchemy import text as sa_text

        ar_row = db.execute(
            sa_text(
                "SELECT "
                "COALESCE(SUM(balance_due) FILTER (WHERE balance_due > 0 "
                "  AND due_at >= NOW() - INTERVAL '30 days'), 0) as ar_30, "
                "COALESCE(SUM(balance_due) FILTER (WHERE balance_due > 0 "
                "  AND due_at < NOW() - INTERVAL '30 days' "
                "  AND due_at >= NOW() - INTERVAL '60 days'), 0) as ar_60 "
                "FROM invoices WHERE is_active = true AND status != 'void'"
            )
        ).one()
        ar_30 = float(ar_row.ar_30)
        ar_60 = float(ar_row.ar_60)
    except Exception:
        logger.debug("Failed to compute AR aging", exc_info=True)

    # --- Bandwidth from device metrics ---
    bw_current = "0"
    bw_peak = "0"
    try:
        from sqlalchemy import text as sa_text

        bw_row = db.execute(
            sa_text(
                "SELECT COALESCE(SUM(value), 0) as total_bps "
                "FROM device_metrics "
                "WHERE metric_type = 'rx_bps' "
                "AND recorded_at > NOW() - INTERVAL '10 minutes' AND value > 0"
            )
        ).one()
        total_bps = float(bw_row.total_bps)
        if total_bps > 1e9:
            bw_current = f"{total_bps / 1e9:.1f} Gbps"
        elif total_bps > 1e6:
            bw_current = f"{total_bps / 1e6:.0f} Mbps"
        else:
            bw_current = f"{total_bps / 1e3:.0f} Kbps"
    except Exception:
        logger.debug("Failed to compute bandwidth stats", exc_info=True)

    stats = {
        "total_subscribers": sub_stats["total_count"],
        "active_subscribers": active_subscribers,
        "subscribers_change": sub_stats.get("new_this_month", 0),
        "monthly_revenue": payments_this_month,
        "mrr": payments_this_month,
        "arpu": arpu,
        "revenue_change": 0,
        "system_uptime": net_stats["uptime_percentage"],
        "ar_current": pending_amount,
        "ar_30": ar_30,
        "ar_60": ar_60,
        "ar_90": overdue_amount,
        "suspended_accounts": sub_stats["suspended_count"],
        "orders_new": 0,
        "orders_qualification": 0,
        "orders_scheduled": 0,
        "orders_in_progress": 0,
        "orders_pending_activation": 0,
        "orders_completed_today": 0,
        "olts_online": olts_online,
        "olts_total": olts_total,
        "onts_active": ont_active,
        "onts_total": ont_total,
        "alarms_critical": net_stats["alarms_critical"],
        "alarms_major": net_stats["alarms_major"],
        "alarms_minor": net_stats["alarms_minor"],
        "alarms_warning": net_stats["alarms_warning"],
        "bandwidth_current": bw_current,
        "bandwidth_peak": bw_peak,
        "churn_rate": sub_stats["churn_rate"],
    }

    # --- Recent activity ---
    recent_activity = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )

    actor_ids = {
        event.actor_id
        for event in recent_activity
        if event.actor_id and _is_user_actor(getattr(event, "actor_type", None))
    }
    subscribers_lookup = {}
    if actor_ids:
        subscribers_lookup = {
            str(subscriber.id): subscriber
            for subscriber in db.query(Subscriber)
            .filter(Subscriber.id.in_(actor_ids))
            .all()
        }

    recent_activities = _build_recent_activities(recent_activity, subscribers_lookup)

    sidebar_stats = web_admin_service.get_sidebar_stats(db)
    current_user = web_admin_service.get_current_user(request)

    # --- Permission-gated sections ---
    from app.services.auth_dependencies import has_permission

    # Permission-gated sections.
    # Web session users: check via RBAC if auth state present, else default True
    # (web admin middleware already verified authentication)
    auth = getattr(request.state, "auth", None) or {}
    user = getattr(request.state, "user", None)

    if auth.get("principal_id"):
        # API-style auth with explicit principal
        def _has(perm: str) -> bool:
            return has_permission(auth, db, perm)

        show_financials = _has("billing:read")
        show_network = _has("network:read") or _has("monitoring:read")
        show_subscribers = _has("subscriber:read")
    elif user:
        # Web session auth — check user's role permissions
        try:
            from app.models.system_user import SystemUser

            sys_user = db.get(
                SystemUser, str(user.get("subscriber_id") or user.get("id", ""))
            )
            roles = getattr(sys_user, "roles", None)
            if sys_user and roles is not None:
                role_names = {getattr(r, "name", "") for r in roles} if roles else set()
                # Admin role sees everything
                is_admin = "admin" in role_names or "super_admin" in role_names
                show_financials = (
                    is_admin or "finance" in role_names or "billing" in role_names
                )
                show_network = (
                    is_admin
                    or "noc" in role_names
                    or "network" in role_names
                    or "technician" in role_names
                )
                show_subscribers = (
                    is_admin or "support" in role_names or "sales" in role_names
                )
            else:
                # No role info — show everything (admin default)
                show_financials = True
                show_network = True
                show_subscribers = True
        except Exception:
            show_financials = True
            show_network = True
            show_subscribers = True
    else:
        # Fallback: show everything
        show_financials = True
        show_network = True
        show_subscribers = True

    # --- Who's Online (RADIUS active sessions) ---
    try:
        from app.models.radius_active_session import RadiusActiveSession

        online_count = db.query(func.count(RadiusActiveSession.id)).scalar() or 0
    except Exception:
        online_count = 0

    # --- Sync status ---
    try:
        from app.models.splynx_mapping import SplynxIdMapping

        last_sync = db.query(func.max(SplynxIdMapping.created_at)).scalar()
        total_mappings = db.query(func.count(SplynxIdMapping.id)).scalar() or 0
        sync_status = {
            "last_sync": last_sync,
            "total_mappings": total_mappings,
            "is_healthy": (
                last_sync is not None
                and (
                    datetime.now(UTC)
                    - (
                        last_sync
                        if last_sync.tzinfo is not None
                        else last_sync.replace(tzinfo=UTC)
                    )
                ).total_seconds()
                < 7200
            )
            if last_sync
            else False,
        }
    except Exception:
        logger.debug("Failed to load sync status for dashboard", exc_info=True)
        sync_status = {"last_sync": None, "total_mappings": 0, "is_healthy": False}

    # --- Monitoring device summary (for operations dashboard) ---
    monitoring_summary = {
        "devices_online": net_stats.get("online_count", 0),
        "devices_offline": net_stats.get("offline_count", 0),
        "devices_degraded": net_stats.get("degraded_count", 0),
        "devices_total": net_stats.get("total_count", 0),
    }

    # --- ONT status summary ---
    try:
        ont_service_summary = network_monitoring_service.get_onu_status_summary(db)
        ont_olt_link_summary = network_monitoring_service.get_onu_olt_status_summary(db)
    except Exception:
        logger.debug("Failed to load ONT summary for dashboard", exc_info=True)
        ont_service_summary = {"online": 0, "offline": 0, "low_signal": 0, "total": 0}
        ont_olt_link_summary = {"online": 0, "offline": 0, "unknown": 0, "total": 0}

    # --- PON interface status summary ---
    try:
        pon_interface_summary = _build_pon_interface_summary(db)
    except Exception:
        logger.debug(
            "Failed to load PON interface summary for dashboard", exc_info=True
        )
        pon_interface_summary = {"up": 0, "down": 0, "unknown": 0, "total": 0}

    # --- VPN tunnel status ---
    vpn_tunnels = []
    try:
        from app.services.web_network_monitoring import get_vpn_tunnel_status

        vpn_tunnels = get_vpn_tunnel_status()
    except Exception:
        logger.debug("Failed to load VPN tunnel status for dashboard", exc_info=True)

    # --- Pending service orders ---
    pending_orders = 0
    try:
        from app.services.provisioning_managers import service_orders

        so_stats = service_orders.get_dashboard_stats(db)
        pending_orders = so_stats.get("pending", 0) + so_stats.get("in_progress", 0)
        stats["orders_new"] = so_stats.get("pending", 0)
        stats["orders_in_progress"] = so_stats.get("in_progress", 0)
        stats["orders_completed_today"] = so_stats.get("completed", 0)
    except ImportError:
        logger.debug(
            "provisioning_managers not available, skipping service order stats"
        )
    except Exception:
        logger.error("Failed to load service order stats for dashboard", exc_info=True)

    # --- Attention items (things needing action) ---
    attention_items: list[dict] = []
    total_alarms = (
        net_stats["alarms_critical"]
        + net_stats["alarms_major"]
        + net_stats["alarms_minor"]
        + net_stats["alarms_warning"]
    )
    if net_stats["alarms_critical"] > 0:
        attention_items.append(
            {
                "label": f"{net_stats['alarms_critical']} critical alarm{'s' if net_stats['alarms_critical'] != 1 else ''}",
                "href": "/admin/network/alarms",
                "severity": "critical",
            }
        )
    if net_stats["alarms_major"] > 0:
        attention_items.append(
            {
                "label": f"{net_stats['alarms_major']} major alarm{'s' if net_stats['alarms_major'] != 1 else ''}",
                "href": "/admin/network/alarms",
                "severity": "major",
            }
        )
    if net_stats.get("offline_count", 0) > 0:
        attention_items.append(
            {
                "label": f"{net_stats['offline_count']} device{'s' if net_stats['offline_count'] != 1 else ''} offline",
                "href": "/admin/network/monitoring",
                "severity": "warning",
            }
        )
    if overdue_amount > 0:
        attention_items.append(
            {
                "label": f"₦{overdue_amount:,.0f} overdue receivables",
                "href": "/admin/billing",
                "severity": "warning",
            }
        )
    if sub_stats["suspended_count"] > 0:
        attention_items.append(
            {
                "label": f"{sub_stats['suspended_count']} suspended account{'s' if sub_stats['suspended_count'] != 1 else ''}",
                "href": "/admin/customers",
                "severity": "info",
            }
        )
    if pending_orders > 0:
        attention_items.append(
            {
                "label": f"{pending_orders} pending service order{'s' if pending_orders != 1 else ''}",
                "href": "/admin/provisioning",
                "severity": "info",
            }
        )

    return templates.TemplateResponse(
        "admin/dashboard/index.html",
        {
            "request": request,
            "stats": stats,
            "subscriber_stats": sub_stats,
            "network_stats": net_stats,
            "billing_stats": billing_stats,
            "network_health": {
                "percent": health_pct,
                "status": health_status,
                "warn_pct": warn_pct,
                "crit_pct": crit_pct,
            },
            "recent_activity": recent_activity,
            "recent_activities": recent_activities,
            "recent_subscribers": sub_stats["recent_subscribers"],
            "active_alarms": net_stats["active_alarms"],
            "attention_items": attention_items,
            "pending_orders": pending_orders,
            "total_alarms": total_alarms,
            "now": datetime.now(),
            "active_page": "dashboard",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "server_health": server_health,
            "server_health_status": server_health_status,
            "online_count": online_count,
            "sync_status": sync_status,
            "show_financials": show_financials,
            "show_network": show_network,
            "show_subscribers": show_subscribers,
            "monitoring_summary": monitoring_summary,
            "ont_service_summary": ont_service_summary,
            "ont_olt_link_summary": ont_olt_link_summary,
            "pon_interface_summary": pon_interface_summary,
            "vpn_tunnels": vpn_tunnels,
        },
    )


def dashboard_server_health_partial(request: Request, db: Session):
    server_health = system_health_service.get_system_health()
    thresholds = _build_health_thresholds(db)
    server_health_status = system_health_service.evaluate_health(
        server_health, thresholds
    )
    return templates.TemplateResponse(
        "admin/dashboard/_server_health.html",
        {
            "request": request,
            "server_health": server_health,
            "server_health_status": server_health_status,
        },
    )


def _get_cached_dashboard_stats(db: Session) -> dict:
    """Get dashboard stats with Redis caching (30 second TTL)."""
    import json

    from app.services.settings_cache import get_settings_redis

    cache_key = "dashboard:stats_partial"
    try:
        r = get_settings_redis()
        if r is not None:
            cached = r.get(cache_key)
            if cached and isinstance(cached, (str, bytes)):
                return json.loads(cached)
    except Exception:
        logger.debug("Dashboard cache read failed", exc_info=True)

    # Cache miss - compute stats
    sub_stats = subscriber_service.subscribers.get_dashboard_stats(db)
    pon_interface_summary = _build_pon_interface_summary(db)

    monthly_revenue = 0
    try:
        from app.services import billing as _billing_svc

        b_stats = _billing_svc.billing_reporting.get_dashboard_stats(db)
        monthly_revenue = b_stats.get("stats", {}).get("payments_amount", 0)
    except Exception:
        logger.debug("Failed to load billing dashboard stats", exc_info=True)

    system_uptime = 0.0
    try:
        from app.services import network_monitoring as _net_mon_svc

        n_stats = _net_mon_svc.network_devices.get_dashboard_stats(db)
        system_uptime = n_stats.get("uptime_percentage", 0.0)
    except Exception:
        logger.debug("Failed to load network monitoring dashboard stats", exc_info=True)

    stats = {
        "total_subscribers": sub_stats["total_count"],
        "active_subscribers": sub_stats["active_count"],
        "subscribers_change": sub_stats.get("new_this_month", 0),
        "monthly_revenue": monthly_revenue,
        "revenue_change": 0,
        "system_uptime": system_uptime,
        "pon_interfaces_up": pon_interface_summary["up"],
        "pon_interfaces_down": pon_interface_summary["down"],
        "pon_interfaces_unknown": pon_interface_summary["unknown"],
        "pon_interfaces_total": pon_interface_summary["total"],
    }

    # Cache for 30 seconds
    try:
        r = get_settings_redis()
        if r is not None:
            r.setex(cache_key, 30, json.dumps(stats))
    except Exception:
        logger.debug("Dashboard cache write failed", exc_info=True)

    return stats


def dashboard_stats_partial(request: Request, db: Session):
    stats = _get_cached_dashboard_stats(db)
    return templates.TemplateResponse(
        "admin/dashboard/_stats.html",
        {"request": request, "stats": stats},
    )


def dashboard_activity_partial(request: Request, db: Session):
    recent_activity = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )

    return templates.TemplateResponse(
        "admin/dashboard/_activity.html",
        {"request": request, "recent_activity": recent_activity},
    )
