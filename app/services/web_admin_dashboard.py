"""Service helpers for admin dashboard routes."""

from datetime import datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntUnit
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


def _build_health_thresholds(db: Session) -> dict:
    """Resolve network/server health thresholds from settings."""
    return {
        "disk_warn_pct": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_warn_pct"
        )),
        "disk_crit_pct": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_crit_pct"
        )),
        "mem_warn_pct": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_warn_pct"
        )),
        "mem_crit_pct": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_crit_pct"
        )),
        "load_warn": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_warn"
        )),
        "load_crit": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_crit"
        )),
        "network_warn_pct": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "network_health_warn_pct"
        )),
        "network_crit_pct": _float_setting(settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "network_health_crit_pct"
        )),
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
                metadata.get("actor_name")
                or metadata.get("actor_email")
                or (str(event.actor_id) if event.actor_id else None)
                or "System"
            )

        time_str = format_audit_datetime(getattr(event, "occurred_at", None), "%H:%M")

        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(action)
        entity_label = humanize_entity(entity_type, entity_id)

        message = f"{actor_name} {action_label} {entity_label}"
        detail = change_summary or entity_label

        recent_activities.append({
            "type": activity_type,
            "message": message,
            "detail": detail,
            "time": time_str,
        })
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
        db.query(func.count(OntUnit.id))
        .filter(OntUnit.is_active.is_(True))
        .scalar()
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
    paid_revenue = b_stats.get("total_revenue", 0)
    pending_amount = b_stats.get("pending_amount", 0)
    overdue_amount = b_stats.get("overdue_amount", 0)
    active_subscribers = sub_stats["active_count"]
    arpu = paid_revenue / active_subscribers if active_subscribers > 0 else 0

    stats = {
        "total_subscribers": sub_stats["total_count"],
        "active_subscribers": active_subscribers,
        "subscribers_change": sub_stats.get("new_this_month", 0),
        "monthly_revenue": paid_revenue,
        "mrr": paid_revenue,
        "arpu": arpu,
        "revenue_change": 0,
        "system_uptime": net_stats["uptime_percentage"],
        "ar_current": pending_amount,
        "ar_30": 0,
        "ar_60": 0,
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
        "bandwidth_current": "0",
        "bandwidth_peak": "0",
        "bandwidth_capacity": "0",
        "jobs_completed": 0,
        "jobs_total": 0,
        "techs_active": 0,
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
            for subscriber in db.query(Subscriber).filter(
                Subscriber.id.in_(actor_ids)
            ).all()
        }

    recent_activities = _build_recent_activities(recent_activity, subscribers_lookup)

    sidebar_stats = web_admin_service.get_sidebar_stats(db)
    current_user = web_admin_service.get_current_user(request)

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
            "todays_jobs": [],
            "now": datetime.now(),
            "active_page": "dashboard",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "server_health": server_health,
            "server_health_status": server_health_status,
        },
    )


def dashboard_server_health_partial(request: Request, db: Session):
    server_health = system_health_service.get_system_health()
    thresholds = _build_health_thresholds(db)
    server_health_status = system_health_service.evaluate_health(server_health, thresholds)
    return templates.TemplateResponse(
        "admin/dashboard/_server_health.html",
        {
            "request": request,
            "server_health": server_health,
            "server_health_status": server_health_status,
        },
    )


def dashboard_stats_partial(request: Request, db: Session):
    sub_stats = subscriber_service.subscribers.get_dashboard_stats(db)

    stats = {
        "total_subscribers": sub_stats["total_count"],
        "active_subscribers": sub_stats["active_count"],
        "subscribers_change": sub_stats.get("new_this_month", 0),
        "monthly_revenue": 0,
        "revenue_change": 0,
        "system_uptime": 99.9,
    }

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
