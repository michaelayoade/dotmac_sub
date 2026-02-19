"""Service helpers for admin dashboard routes."""

from datetime import datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import InvoiceStatus
from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntUnit
from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.models.subscriber import Subscriber
from app.services import (
    audit as audit_service,
)
from app.services import (
    billing as billing_service,
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


def dashboard(request: Request, db: Session):
    server_health = system_health_service.get_system_health()
    thresholds = {
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
    server_health_status = system_health_service.evaluate_health(server_health, thresholds)

    olt_total = db.query(OLTDevice).count()
    olt_online = (
        db.query(OLTDevice).filter(OLTDevice.is_active.is_(True)).count()
    )
    ont_total = db.query(OntUnit).count()
    ont_active = (
        db.query(OntUnit).filter(OntUnit.is_active.is_(True)).count()
    )
    monitoring_total = (
        db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).count()
    )
    monitoring_online = (
        db.query(NetworkDevice)
        .filter(NetworkDevice.is_active.is_(True))
        .filter(
            NetworkDevice.status.in_(
                [DeviceStatus.online, DeviceStatus.degraded, DeviceStatus.maintenance]
            )
        )
        .count()
    )
    # Default to inventory counts, but fall back to monitoring devices if no OLTs are defined.
    olts_total = monitoring_total if olt_total == 0 and monitoring_total > 0 else olt_total
    olts_online = (
        monitoring_online if olt_total == 0 and monitoring_total > 0 else olt_online
    )
    onts_total = ont_total
    onts_active = ont_active

    subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
                organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    total_subscribers = len(subscribers)
    active_subscribers = sum(1 for s in subscribers if getattr(s, "is_active", True))

    invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )

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

    recent_subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=5,
        offset=0,
    )

    all_invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    paid_revenue = sum(
        _invoice_total(inv) for inv in all_invoices if inv.status == InvoiceStatus.paid
    )
    pending_amount = sum(
        _invoice_total(inv) for inv in all_invoices if inv.status == InvoiceStatus.issued
    )
    overdue_amount = sum(
        _invoice_total(inv) for inv in all_invoices if inv.status == InvoiceStatus.overdue
    )

    arpu = paid_revenue / active_subscribers if active_subscribers > 0 else 0

    health_pct = int((olts_online / olts_total) * 100) if olts_total > 0 else 0
    warn_pct = thresholds.get("network_warn_pct") or 90
    crit_pct = thresholds.get("network_crit_pct") or 70
    if health_pct >= warn_pct:
        health_status = "healthy"
    elif health_pct >= crit_pct:
        health_status = "warning"
    else:
        health_status = "critical"

    stats = {
        "total_subscribers": total_subscribers,
        "active_subscribers": active_subscribers,
        "subscribers_change": 0,
        "monthly_revenue": paid_revenue,
        "mrr": paid_revenue,
        "arpu": arpu,
        "revenue_change": 0,
        "system_uptime": 99.9,
        "ar_current": pending_amount,
        "ar_30": 0,
        "ar_60": 0,
        "ar_90": overdue_amount,
        "suspended_accounts": 0,
        "orders_new": 0,
        "orders_qualification": 0,
        "orders_scheduled": 0,
        "orders_in_progress": 0,
        "orders_pending_activation": 0,
        "orders_completed_today": 0,
        "olts_online": olts_online,
        "olts_total": olts_total,
        "onts_active": onts_active,
        "onts_total": onts_total,
        "alarms_critical": 0,
        "alarms_major": 0,
        "alarms_minor": 0,
        "alarms_warning": 0,
        "bandwidth_current": "0",
        "bandwidth_peak": "0",
        "bandwidth_capacity": "0",
        "jobs_completed": 0,
        "jobs_total": 0,
        "techs_active": 0,
        "churn_rate": 0,
    }

    actor_ids = {
        event.actor_id
        for event in recent_activity
        if event.actor_id and _is_user_actor(getattr(event, "actor_type", None))
    }
    subscribers_lookup = {}
    if actor_ids:
        subscribers_lookup = {
            str(subscriber.id): subscriber
            for subscriber in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }

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

    sidebar_stats = web_admin_service.get_sidebar_stats(db)
    current_user = web_admin_service.get_current_user(request)

    return templates.TemplateResponse(
        "admin/dashboard/index.html",
        {
            "request": request,
            "stats": stats,
            "network_health": {
                "percent": health_pct,
                "status": health_status,
                "warn_pct": warn_pct,
                "crit_pct": crit_pct,
            },
            "recent_activity": recent_activity,
            "recent_activities": recent_activities,
            "recent_subscribers": recent_subscribers,
            "active_alarms": [],
            "todays_jobs": [],
            "now": datetime.now(),
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "server_health": server_health,
            "server_health_status": server_health_status,
        },
    )


def dashboard_server_health_partial(request: Request, db: Session):
    server_health = system_health_service.get_system_health()
    thresholds = {
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
    }
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
    subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    stats = {
        "total_subscribers": len(subscribers),
        "active_subscribers": sum(1 for s in subscribers if getattr(s, "is_active", True)),
        "subscribers_change": 12,
        "monthly_revenue": 45231.89,
        "revenue_change": 8,
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
