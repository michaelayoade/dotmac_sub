"""Service helpers for admin web layer."""

from __future__ import annotations

import logging
from threading import Lock
from time import monotonic

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.notification import Notification, NotificationStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus

logger = logging.getLogger(__name__)

def _get_initials(name: str) -> str:
    if not name:
        return "??"
    parts = name.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return name[0:2].upper()


def get_current_user(request) -> dict:
    """Get current user context from the request state."""
    if hasattr(request.state, "user") and request.state.user:
        user = request.state.user
        name = f"{user.first_name} {user.last_name}".strip() if hasattr(user, "first_name") else "User"
        person_id = getattr(user, "person_id", None)
        subscriber_id = str(person_id if person_id else getattr(user, "id", ""))
        return {
            "id": str(getattr(user, "id", "")),
            "person_id": subscriber_id,
            "subscriber_id": subscriber_id,
            "initials": _get_initials(name),
            "name": name,
            "email": getattr(user, "email", ""),
        }

    return {
        "id": "",
        "person_id": "",
        "initials": "??",
        "name": "Unknown User",
        "email": "",
    }


_SIDEBAR_STATS_TTL_SECONDS = 10.0
_sidebar_stats_lock = Lock()
_sidebar_stats_cached_at = 0.0
_sidebar_stats_cache: dict[str, object] | None = None


def _count_open_service_orders(db: Session) -> int:
    """Count non-terminal service orders without loading full rows."""
    return (
        db.query(func.count(ServiceOrder.id))
        .filter(
            ServiceOrder.status.notin_(
                (
                    ServiceOrderStatus.active,
                    ServiceOrderStatus.canceled,
                    ServiceOrderStatus.failed,
                )
            )
        )
        .scalar()
        or 0
    )


def _count_unread_notifications(db: Session) -> int:
    """Count pending notification queue items for top-bar indicator."""
    return (
        db.query(func.count(Notification.id))
        .filter(Notification.is_active.is_(True))
        .filter(Notification.status.in_((NotificationStatus.queued, NotificationStatus.sending)))
        .scalar()
        or 0
    )


def get_sidebar_stats(db: Session) -> dict:
    """Get stats for sidebar badges."""
    global _sidebar_stats_cached_at, _sidebar_stats_cache

    from app.models.domain_settings import SettingDomain
    from app.services import module_manager as module_manager_service
    from app.services import settings_spec

    now = monotonic()
    with _sidebar_stats_lock:
        if _sidebar_stats_cache and (now - _sidebar_stats_cached_at) < _SIDEBAR_STATS_TTL_SECONDS:
            return dict(_sidebar_stats_cache)

    try:
        service_orders_count = _count_open_service_orders(db)
    except Exception:
        service_orders_count = 0
    try:
        notifications_unread = _count_unread_notifications(db)
    except Exception:
        notifications_unread = 0

    try:
        logo_raw = settings_spec.resolve_value(db, SettingDomain.comms, "sidebar_logo_url")
        sidebar_logo_url = str(logo_raw).strip() if logo_raw else ""
    except Exception:
        sidebar_logo_url = ""
    try:
        dark_logo_raw = settings_spec.resolve_value(db, SettingDomain.comms, "sidebar_logo_dark_url")
        sidebar_logo_dark_url = str(dark_logo_raw).strip() if dark_logo_raw else ""
    except Exception:
        sidebar_logo_dark_url = ""
    try:
        favicon_raw = settings_spec.resolve_value(db, SettingDomain.comms, "favicon_url")
        favicon_url = str(favicon_raw).strip() if favicon_raw else ""
    except Exception:
        favicon_url = ""
    try:
        from app.services import web_system_company_info as web_system_company_info_service

        app_name = (web_system_company_info_service.get_company_info(db).get("company_name") or "").strip()
    except Exception:
        app_name = ""
    try:
        module_states = module_manager_service.load_module_states(db)
        feature_states = module_manager_service.load_feature_states(db)
    except Exception:
        module_states = {}
        feature_states = {}

    stats = {
        "service_orders": service_orders_count,
        "dispatch_jobs": 0,
        "notifications_unread": notifications_unread,
        "sidebar_logo_url": sidebar_logo_url,
        "sidebar_logo_dark_url": sidebar_logo_dark_url,
        "favicon_url": favicon_url,
        "app_name": app_name,
        "module_states": module_states,
        "feature_states": feature_states,
    }
    with _sidebar_stats_lock:
        _sidebar_stats_cached_at = monotonic()
        _sidebar_stats_cache = dict(stats)

    return stats
