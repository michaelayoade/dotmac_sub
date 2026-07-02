"""Service helpers for admin dashboard routes."""

import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from threading import Lock
from time import monotonic

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.metrics import (
    observe_cache_refresh,
    record_cache_fallback,
    record_cache_lookup,
)
from app.models.audit import AuditActorType
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.network import OLTDevice, OntUnit, OnuOnlineStatus
from app.models.network_monitoring import (
    DeviceInterface,
    InterfaceStatus,
    NetworkDevice,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.models.subscriber import Subscriber
from app.services import admin_alerts as admin_alerts_service
from app.services import admin_whats_new as admin_whats_new_service
from app.services import app_cache, settings_spec
from app.services import infrastructure_health as infrastructure_health_service
from app.services import (
    subscriber as subscriber_service,
)
from app.services import (
    system_health as system_health_service,
)
from app.services import (
    web_admin as web_admin_service,
)
from app.services import web_system_health as web_system_health_service
from app.services.audit_adapter import audit_adapter
from app.services.audit_helpers import (
    build_recent_activity_feed,
    extract_changes,
    format_audit_datetime,
    format_changes,
    humanize_action,
    humanize_entity,
    resolve_actor_name,
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
_DASHBOARD_STATS_CACHE_TTL_SECONDS = max(
    60, int(os.getenv("DASHBOARD_STATS_CACHE_TTL_SECONDS", "180"))
)
_DASHBOARD_STATS_CACHE_KEY = app_cache.cache_key("dashboard", "stats-summary")

# In-process cache for the main /admin/dashboard global context.
# Contains SQLAlchemy ORM rows (recent_activity, recent_subscribers, active_alarms)
# that can't be JSON-serialized to Redis, hence the in-process cache.
_DASHBOARD_GLOBAL_TTL_SECONDS = float(
    os.getenv("DASHBOARD_GLOBAL_CACHE_TTL_SECONDS", "60")
)
_dashboard_global_lock = Lock()
_dashboard_global_cached_at = 0.0
_dashboard_global_cache: dict[str, object] | None = None

_DASHBOARD_INFRASTRUCTURE_TTL_SECONDS = max(
    5.0, float(os.getenv("DASHBOARD_INFRASTRUCTURE_CACHE_TTL_SECONDS", "60"))
)
_dashboard_infrastructure_lock = Lock()
_dashboard_infrastructure_cached_at = 0.0
_dashboard_infrastructure_cache: (
    tuple[
        list[infrastructure_health_service.ServiceStatus],
        dict[str, object],
        dict[str, int],
    ]
    | None
) = None


def _invoice_total(inv) -> float:
    return float(getattr(inv, "total", None) or getattr(inv, "total_amount", 0) or 0)


def _float_setting(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _setting_raw_value(setting: DomainSetting) -> object | None:
    return setting.value_json if setting.value_json is not None else setting.value_text


def _rollback_after_failed_query(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        logger.debug("Failed to roll back dashboard session", exc_info=True)


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


def _build_pon_outages(db: Session, limit: int = 10) -> list[dict]:
    """Return list of PON interfaces that are currently down.

    Returns up to `limit` interfaces with OLT name and last updated time.
    """
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

    rows = (
        db.query(
            DeviceInterface.id,
            DeviceInterface.name,
            DeviceInterface.description,
            DeviceInterface.updated_at,
            NetworkDevice.id.label("device_id"),
            NetworkDevice.name.label("device_name"),
        )
        .join(NetworkDevice, NetworkDevice.id == DeviceInterface.device_id)
        .filter(NetworkDevice.is_active.is_(True))
        .filter(pon_pattern)
        .filter(DeviceInterface.status == InterfaceStatus.down)
        .order_by(DeviceInterface.updated_at.desc())
        .limit(limit)
        .all()
    )

    outages = []
    for row in rows:
        outages.append(
            {
                "id": str(row.id),
                "name": row.name,
                "description": row.description or "",
                "olt_id": str(row.device_id),
                "olt_name": row.device_name or "Unknown OLT",
                "down_since": row.updated_at,
            }
        )
    return outages


def _build_cached_ont_status_summary(db: Session) -> dict[str, int]:
    """Return ONT status from locally persisted monitoring fields.

    The dashboard must not synchronously poll Zabbix per OLT during initial
    render. Background ingestion keeps these columns fresh enough for overview
    counts, while live diagnostics pages can still query Zabbix directly.
    """
    thresholds = _build_health_thresholds(db)
    low_signal_threshold = thresholds.get("ont_signal_warning_dbm") or -25
    counts = (
        db.query(
            func.count(OntUnit.id).label("total"),
            func.count(OntUnit.id)
            .filter(OntUnit.olt_status == OnuOnlineStatus.online)
            .label("online"),
            func.count(OntUnit.id)
            .filter(OntUnit.olt_rx_signal_dbm.is_not(None))
            .filter(OntUnit.olt_rx_signal_dbm < low_signal_threshold)
            .label("low_signal"),
        )
        .filter(OntUnit.is_active.is_(True))
        .one()
    )
    total = counts.total or 0
    online = counts.online or 0
    low_signal = counts.low_signal or 0
    return {
        "total": total,
        "online": online,
        "offline": max(total - online, 0),
        "low_signal": low_signal,
    }


def _build_health_thresholds(db: Session) -> dict:
    """Resolve network/server health thresholds from settings."""
    keys = {
        "server_health_disk_warn_pct": "disk_warn_pct",
        "server_health_disk_crit_pct": "disk_crit_pct",
        "server_health_mem_warn_pct": "mem_warn_pct",
        "server_health_mem_crit_pct": "mem_crit_pct",
        "server_health_load_warn": "load_warn",
        "server_health_load_crit": "load_crit",
        "network_health_warn_pct": "network_warn_pct",
        "network_health_crit_pct": "network_crit_pct",
        "ont_signal_warning_dbm": "ont_signal_warning_dbm",
    }
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.network_monitoring)
        .filter(DomainSetting.key.in_(keys.keys()))
        .filter(DomainSetting.is_active.is_(True))
        .all()
    )
    values = {keys[row.key]: _float_setting(_setting_raw_value(row)) for row in rows}
    return {field: values.get(field) for field in keys.values()}


def _network_monitoring_int_setting(db: Session, key: str, default: int) -> int:
    raw = settings_spec.resolve_value(db, SettingDomain.network_monitoring, key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _build_dashboard_billing_summary(db: Session) -> dict[str, float]:
    """Return the small billing aggregate needed by the admin overview."""
    from sqlalchemy import text as sa_text

    try:
        row = db.execute(
            sa_text(
                """
                SELECT
                    COALESCE((
                        SELECT SUM(amount)
                        FROM payments
                        WHERE is_active = true
                          AND status = 'succeeded'
                          AND paid_at >= date_trunc('month', NOW())
                          AND paid_at < date_trunc('month', NOW()) + INTERVAL '1 month'
                    ), 0) AS payments_this_month,
                    COALESCE((
                        SELECT SUM(balance_due)
                        FROM invoices
                        WHERE is_active = true
                          AND status != 'void'
                          AND balance_due > 0
                    ), 0) AS pending_amount,
                    COALESCE((
                        SELECT SUM(balance_due)
                        FROM invoices
                        WHERE is_active = true
                          AND status != 'void'
                          AND balance_due > 0
                          AND due_at < NOW()
                    ), 0) AS overdue_amount
                """
            )
        ).one()
        return {
            "payments_this_month": float(row.payments_this_month or 0),
            "pending_amount": float(row.pending_amount or 0),
            "overdue_amount": float(row.overdue_amount or 0),
        }
    except Exception:
        logger.debug("Failed to load dashboard billing summary", exc_info=True)
        _rollback_after_failed_query(db)
        return {
            "payments_this_month": 0.0,
            "pending_amount": 0.0,
            "overdue_amount": 0.0,
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

        actor_name = resolve_actor_name(event, subscribers_lookup)

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


def _build_dashboard_global_context(db: Session) -> dict[str, object]:
    """Build the heavy, non-user-specific portion of the dashboard context.

    Result is cached in-process for ~60s (see _get_cached_dashboard_global_context).
    """
    from app.services import network_monitoring as network_monitoring_service

    # --- Server health ---
    server_health = system_health_service.get_system_health()
    thresholds = _build_health_thresholds(db)
    server_health_status = system_health_service.evaluate_health(
        server_health, thresholds
    )

    # --- Centralized stats ---
    sub_stats = subscriber_service.subscribers.get_dashboard_stats(db)
    net_stats = network_monitoring_service.network_devices.get_dashboard_stats(db)
    billing_summary = _build_dashboard_billing_summary(db)

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
    payments_this_month = billing_summary["payments_this_month"]
    pending_amount = billing_summary["pending_amount"]
    overdue_amount = billing_summary["overdue_amount"]
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
        _rollback_after_failed_query(db)

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
        _rollback_after_failed_query(db)

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
    recent_activity = audit_adapter.list_events(
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
        healthy_age_seconds = _network_monitoring_int_setting(
            db,
            "dashboard_sync_healthy_age_seconds",
            7200,
        )
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
                < healthy_age_seconds
            )
            if last_sync
            else False,
        }
    except Exception:
        logger.debug("Failed to load sync status for dashboard", exc_info=True)
        _rollback_after_failed_query(db)
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
        ont_service_summary = _build_cached_ont_status_summary(db)
        ont_olt_link_summary = dict(ont_service_summary)
    except Exception:
        logger.debug("Failed to load ONT summary for dashboard", exc_info=True)
        _rollback_after_failed_query(db)
        ont_service_summary = {"online": 0, "offline": 0, "low_signal": 0, "total": 0}
        ont_olt_link_summary = {"online": 0, "offline": 0, "total": 0}

    # --- Unconfigured ONTs (autofind candidates) ---
    unconfigured_ont_count = 0
    try:
        unconfigured_ont_count = (
            db.query(func.count(OltAutofindCandidate.id))
            .filter(OltAutofindCandidate.is_active.is_(True))
            .scalar()
            or 0
        )
    except Exception:
        logger.debug(
            "Failed to load unconfigured ONT count for dashboard", exc_info=True
        )
        _rollback_after_failed_query(db)

    # --- PON interface status summary ---
    try:
        pon_interface_summary = _build_pon_interface_summary(db)
    except Exception:
        logger.debug(
            "Failed to load PON interface summary for dashboard", exc_info=True
        )
        _rollback_after_failed_query(db)
        pon_interface_summary = {"up": 0, "down": 0, "unknown": 0, "total": 0}

    # --- PON outages (interfaces currently down) ---
    pon_outages: list[dict] = []
    try:
        pon_outages = _build_pon_outages(db, limit=10)
    except Exception:
        logger.debug("Failed to load PON outages for dashboard", exc_info=True)
        _rollback_after_failed_query(db)

    # --- Pending service orders ---
    pending_orders = 0
    try:
        from app.models.provisioning import ServiceOrder, ServiceOrderStatus

        order_counts = db.query(
            func.count(ServiceOrder.id)
            .filter(
                ServiceOrder.status.in_(
                    (ServiceOrderStatus.submitted, ServiceOrderStatus.scheduled)
                )
            )
            .label("pending"),
            func.count(ServiceOrder.id)
            .filter(ServiceOrder.status == ServiceOrderStatus.provisioning)
            .label("in_progress"),
            func.count(ServiceOrder.id)
            .filter(ServiceOrder.status == ServiceOrderStatus.active)
            .label("completed"),
        ).one()
        pending = order_counts.pending or 0
        in_progress = order_counts.in_progress or 0
        pending_orders = pending + in_progress
        stats["orders_new"] = pending
        stats["orders_in_progress"] = in_progress
        stats["orders_completed_today"] = order_counts.completed or 0
    except Exception:
        logger.error("Failed to load service order stats for dashboard", exc_info=True)
        _rollback_after_failed_query(db)

    # --- Attention items (things needing action) ---
    attention_items: list[dict] = []
    network_attention_items: list[dict] = []
    total_alarms = (
        net_stats["alarms_critical"]
        + net_stats["alarms_major"]
        + net_stats["alarms_minor"]
        + net_stats["alarms_warning"]
    )
    if net_stats["alarms_critical"] > 0:
        item = {
            "label": f"{net_stats['alarms_critical']} critical alarm{'s' if net_stats['alarms_critical'] != 1 else ''}",
            "href": "/admin/network/alarms",
            "severity": "critical",
            "domain": "network",
        }
        attention_items.append(item)
        network_attention_items.append(item)
    if net_stats["alarms_major"] > 0:
        item = {
            "label": f"{net_stats['alarms_major']} major alarm{'s' if net_stats['alarms_major'] != 1 else ''}",
            "href": "/admin/network/alarms",
            "severity": "major",
            "domain": "network",
        }
        attention_items.append(item)
        network_attention_items.append(item)
    if net_stats.get("offline_count", 0) > 0:
        item = {
            "label": f"{net_stats['offline_count']} device{'s' if net_stats['offline_count'] != 1 else ''} offline",
            "href": "/admin/network/monitoring",
            "severity": "warning",
            "domain": "network",
        }
        attention_items.append(item)
        network_attention_items.append(item)
    if overdue_amount > 0:
        attention_items.append(
            {
                "label": f"₦{overdue_amount:,.0f} overdue receivables",
                "href": "/admin/billing",
                "severity": "warning",
                "domain": "billing",
            }
        )
    if sub_stats["suspended_count"] > 0:
        attention_items.append(
            {
                "label": f"{sub_stats['suspended_count']} suspended account{'s' if sub_stats['suspended_count'] != 1 else ''}",
                "href": "/admin/customers",
                "severity": "info",
                "domain": "customers",
            }
        )
    if pending_orders > 0:
        attention_items.append(
            {
                "label": f"{pending_orders} pending service order{'s' if pending_orders != 1 else ''}",
                "href": "/admin/provisioning",
                "severity": "info",
                "domain": "provisioning",
            }
        )

    # --- ONT attention items ---
    ont_low_signal = ont_service_summary.get("low_signal", 0)
    ont_offline = ont_service_summary.get("offline", 0)
    if ont_low_signal > 0:
        item = {
            "label": f"{ont_low_signal} ONT{'s' if ont_low_signal != 1 else ''} with low signal",
            "href": "/admin/network/onts?view=diagnostics&signal_quality=warning&order_by=signal&order_dir=asc",
            "severity": "warning",
            "domain": "network",
        }
        attention_items.append(item)
        network_attention_items.append(item)
    if ont_offline > 5:
        # Only show if significant number of offline ONTs
        item = {
            "label": f"{ont_offline} ONT{'s' if ont_offline != 1 else ''} offline",
            "href": "/admin/network/onts?view=list&olt_status=offline",
            "severity": "warning",
            "domain": "network",
        }
        attention_items.append(item)
        network_attention_items.append(item)
    if unconfigured_ont_count > 0:
        item = {
            "label": f"{unconfigured_ont_count} unconfigured ONT{'s' if unconfigured_ont_count != 1 else ''} awaiting authorization",
            "href": "/admin/network/onts?view=unconfigured",
            "severity": "info",
            "domain": "network",
        }
        attention_items.append(item)
        network_attention_items.append(item)

    try:
        pending_location_requests = web_admin_service._count_pending_location_requests(
            db
        )
    except Exception:
        logger.error(
            "Failed to load pending location requests for dashboard", exc_info=True
        )
        _rollback_after_failed_query(db)
        pending_location_requests = 0
    if pending_location_requests > 0:
        attention_items.append(
            {
                "label": (
                    f"{pending_location_requests} pending pin "
                    f"correction{'s' if pending_location_requests != 1 else ''}"
                ),
                "href": "/admin/gis?tab=customer-requests&status=pending",
                "severity": "info",
                "domain": "customers",
            }
        )

    whats_new_items = admin_whats_new_service.serialize_for_dashboard(
        admin_whats_new_service.get_visible_items(db, limit=4)
    )
    admin_alert_summary = admin_alerts_service.dashboard_alert_summary(db)

    return {
        "stats": stats,
        "subscriber_stats": sub_stats,
        "network_stats": net_stats,
        "billing_stats": {"stats": billing_summary},
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
        "network_attention_items": network_attention_items,
        "admin_alert_summary": admin_alert_summary,
        "pending_orders": pending_orders,
        "total_alarms": total_alarms,
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
        "server_health": server_health,
        "server_health_status": server_health_status,
        "online_count": online_count,
        "sync_status": sync_status,
        "monitoring_summary": monitoring_summary,
        "ont_service_summary": ont_service_summary,
        "ont_olt_link_summary": ont_olt_link_summary,
        "pon_interface_summary": pon_interface_summary,
        "pon_outages": pon_outages,
        "vpn_tunnels": [],
        "whats_new_items": whats_new_items,
        "unconfigured_ont_count": unconfigured_ont_count,
    }


def _get_cached_dashboard_global_context(db: Session) -> dict[str, object]:
    """Return cached dashboard global context, recomputing on TTL expiry.

    Single-flight: first thread holds the lock during recompute so concurrent
    requests don't all stampede the remote DB after cache expiry.
    """
    global _dashboard_global_cached_at, _dashboard_global_cache

    now = monotonic()
    if (
        _dashboard_global_cache
        and (now - _dashboard_global_cached_at) < _DASHBOARD_GLOBAL_TTL_SECONDS
    ):
        return _dashboard_global_cache

    with _dashboard_global_lock:
        now = monotonic()
        if (
            _dashboard_global_cache
            and (now - _dashboard_global_cached_at) < _DASHBOARD_GLOBAL_TTL_SECONDS
        ):
            return _dashboard_global_cache
        fresh = _build_dashboard_global_context(db)
        _dashboard_global_cached_at = monotonic()
        _dashboard_global_cache = fresh
        return fresh


def _resolve_dashboard_permissions(
    request: Request, db: Session
) -> tuple[bool, bool, bool]:
    """Resolve per-user show_financials/show_network/show_subscribers flags."""
    from app.services.auth_dependencies import has_permission

    auth = getattr(request.state, "auth", None) or {}
    user = getattr(request.state, "user", None)

    if auth.get("principal_id"):

        def _has(perm: str) -> bool:
            return has_permission(auth, db, perm)

        return (
            _has("billing:invoice:read")
            or _has("billing:payment:read")
            or _has("reports:billing"),
            _has("network:device:read")
            or _has("network:olt:read")
            or _has("network:ont:read")
            or _has("monitoring:read")
            or _has("reports:network"),
            _has("customer:read"),
        )
    if user:
        try:
            from app.models.system_user import SystemUser

            sys_user = db.get(
                SystemUser, str(user.get("subscriber_id") or user.get("id", ""))
            )
            roles = getattr(sys_user, "roles", None)
            if sys_user and roles is not None:
                role_names = {getattr(r, "name", "") for r in roles} if roles else set()
                is_admin = "admin" in role_names or "super_admin" in role_names
                return (
                    is_admin or "finance" in role_names or "billing" in role_names,
                    is_admin
                    or "noc" in role_names
                    or "network" in role_names
                    or "technician" in role_names,
                    is_admin or "support" in role_names or "sales" in role_names,
                )
        except Exception:
            pass
    return (True, True, True)


def dashboard(request: Request, db: Session):
    """Build the main admin dashboard context and return TemplateResponse.

    Heavy global queries are memoized via _get_cached_dashboard_global_context().
    Only per-user fields (current_user + permission flags) are computed inline.
    """
    show_financials, show_network, show_subscribers = _resolve_dashboard_permissions(
        request, db
    )
    current_user = web_admin_service.get_current_user(request)
    global_ctx = _get_cached_dashboard_global_context(db)

    return templates.TemplateResponse(
        "admin/dashboard/index.html",
        {
            "request": request,
            "now": datetime.now(UTC),
            "data_as_of": datetime.now(UTC),
            "active_page": "dashboard",
            "current_user": current_user,
            "show_financials": show_financials,
            "show_network": show_network,
            "show_subscribers": show_subscribers,
            **global_ctx,
        },
    )


def dashboard_server_health_partial(
    request: Request,
    db: Session,
    worker_action_notice: dict[str, str] | None = None,
):
    server_health = system_health_service.get_system_health()
    thresholds = _build_health_thresholds(db)
    server_health_status = system_health_service.evaluate_health(
        server_health, thresholds
    )
    try:
        (
            infrastructure_services,
            worker_health,
            service_summary,
        ) = _load_dashboard_infrastructure_health(db)
    except Exception:
        logger.exception("Failed to load dashboard infrastructure health")
        infrastructure_services = []
        worker_health = web_system_health_service._build_worker_health([])
        service_summary = {
            "total": 0,
            "up": 0,
            "degraded": 0,
            "down": 0,
            "unknown": 0,
        }
    return templates.TemplateResponse(
        "admin/dashboard/_server_health.html",
        {
            "request": request,
            "server_health": server_health,
            "server_health_status": server_health_status,
            "infrastructure_services": infrastructure_services,
            "worker_health": worker_health,
            "service_summary": service_summary,
            "worker_action_notice": worker_action_notice,
        },
    )


def clear_dashboard_infrastructure_cache() -> None:
    global _dashboard_infrastructure_cached_at, _dashboard_infrastructure_cache
    with _dashboard_infrastructure_lock:
        _dashboard_infrastructure_cache = None
        _dashboard_infrastructure_cached_at = 0.0


def _load_dashboard_infrastructure_health(
    db: Session,
) -> tuple[
    list[infrastructure_health_service.ServiceStatus],
    dict[str, object],
    dict[str, int],
]:
    """Return the infrastructure dashboard snapshot with a short process cache."""
    global _dashboard_infrastructure_cached_at, _dashboard_infrastructure_cache

    now = monotonic()
    cached = _dashboard_infrastructure_cache
    if (
        cached is not None
        and now - _dashboard_infrastructure_cached_at
        < _DASHBOARD_INFRASTRUCTURE_TTL_SECONDS
    ):
        return cached

    with _dashboard_infrastructure_lock:
        now = monotonic()
        cached = _dashboard_infrastructure_cache
        if (
            cached is not None
            and now - _dashboard_infrastructure_cached_at
            < _DASHBOARD_INFRASTRUCTURE_TTL_SECONDS
        ):
            return cached

        infrastructure_services = infrastructure_health_service.check_all_services(db)
        worker_health = web_system_health_service._build_worker_health(
            infrastructure_services
        )
        service_summary = _build_infrastructure_service_summary(infrastructure_services)
        snapshot = (
            infrastructure_services,
            worker_health,
            service_summary,
        )
        _dashboard_infrastructure_cache = snapshot
        _dashboard_infrastructure_cached_at = now
        return snapshot


def _build_infrastructure_service_summary(
    services: Sequence[object],
) -> dict[str, int]:
    summary = {
        "total": len(services),
        "up": 0,
        "degraded": 0,
        "down": 0,
        "unknown": 0,
    }
    for service in services:
        status = str(getattr(service, "status", "unknown") or "unknown").lower()
        if status in {"up", "healthy", "ok", "streaming"}:
            summary["up"] += 1
        elif status in {"degraded", "partial", "warning"}:
            summary["degraded"] += 1
        elif status in {"down", "critical", "failed"}:
            summary["down"] += 1
        else:
            summary["unknown"] += 1
    return summary


def _build_dashboard_stats_summary(db: Session) -> dict:
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

    return {
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


def refresh_dashboard_stats_cache(db: Session) -> dict:
    started_at = monotonic()
    try:
        stats = _build_dashboard_stats_summary(db)
        app_cache.set_json(
            _DASHBOARD_STATS_CACHE_KEY,
            stats,
            _DASHBOARD_STATS_CACHE_TTL_SECONDS,
        )
        observe_cache_refresh(
            "dashboard_stats_summary",
            "success",
            monotonic() - started_at,
        )
        return stats
    except Exception:
        observe_cache_refresh(
            "dashboard_stats_summary",
            "failure",
            monotonic() - started_at,
        )
        raise


def _get_cached_dashboard_stats(db: Session) -> dict:
    cached = app_cache.get_json(_DASHBOARD_STATS_CACHE_KEY)
    if isinstance(cached, dict):
        record_cache_lookup("dashboard_stats_summary", "hit")
        return cached

    record_cache_lookup("dashboard_stats_summary", "miss")
    record_cache_fallback("dashboard_stats_summary", "sync_recompute")
    try:
        return refresh_dashboard_stats_cache(db)
    except Exception:
        logger.debug("Dashboard cache refresh failed", exc_info=True)
        return _build_dashboard_stats_summary(db)


def dashboard_stats_partial(request: Request, db: Session):
    show_financials, show_network, show_subscribers = _resolve_dashboard_permissions(
        request, db
    )
    global_ctx = _get_cached_dashboard_global_context(db)
    return templates.TemplateResponse(
        "admin/dashboard/_stats.html",
        {
            "request": request,
            "show_financials": show_financials,
            "show_network": show_network,
            "show_subscribers": show_subscribers,
            **global_ctx,
        },
    )


def dashboard_activity_partial(request: Request, db: Session):
    recent_activity = audit_adapter.list_events(
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
    recent_activities = build_recent_activity_feed(db, recent_activity, limit=10)

    return templates.TemplateResponse(
        "admin/dashboard/_activity.html",
        {"request": request, "recent_activities": recent_activities},
    )
