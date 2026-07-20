"""Service helpers for admin web layer."""

from __future__ import annotations

import logging
from threading import Lock
from time import monotonic
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.gis import (
    CustomerLocationChangeRequest,
    CustomerLocationChangeRequestStatus,
)
from app.models.notification import Notification, NotificationStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.subscriber import Subscriber
from app.services import admin_alerts as admin_alerts_service

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
        principal_type = (
            getattr(request.state, "auth", {}).get("principal_type", "subscriber")
            if hasattr(request.state, "auth")
            else "subscriber"
        )
        name = (
            f"{user.first_name} {user.last_name}".strip()
            if hasattr(user, "first_name")
            else "User"
        )
        principal_id = str(getattr(user, "id", ""))
        person_id = getattr(user, "person_id", None)
        subscriber_id = (
            ""
            if principal_type == "system_user"
            else str(person_id if person_id else principal_id)
        )
        return {
            "id": principal_id,
            "actor_id": principal_id,
            "principal_id": principal_id,
            "person_id": "" if principal_type == "system_user" else subscriber_id,
            "subscriber_id": subscriber_id,
            "principal_type": principal_type,
            "initials": _get_initials(name),
            "name": name,
            "email": getattr(user, "email", ""),
        }

    return {
        "id": "",
        "actor_id": "",
        "principal_id": "",
        "person_id": "",
        "subscriber_id": "",
        "initials": "??",
        "name": "Unknown User",
        "email": "",
    }


def get_actor_id(request: object) -> str | None:
    """Extract actor ID from the current authenticated user."""
    current_user = get_current_user(request)
    value = (
        current_user.get("actor_id")
        or current_user.get("principal_id")
        or current_user.get("id")
        if current_user
        else None
    )
    return str(value) if value else None


def get_uploaded_by_subscriber_id(request: object, db: Session) -> str | None:
    """Return a subscriber owner for uploads, excluding system-user principals."""
    current_user = get_current_user(request)
    if not current_user or current_user.get("principal_type") == "system_user":
        return None
    candidate = str(current_user.get("subscriber_id") or "").strip()
    if not candidate:
        return None
    try:
        subscriber_id = UUID(candidate)
    except ValueError:
        return None
    return str(subscriber_id) if db.get(Subscriber, subscriber_id) else None


_SIDEBAR_STATS_TTL_SECONDS = 60.0
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
    """Count unread admin alerts plus pending notification queue items."""
    queued = (
        db.query(func.count(Notification.id))
        .filter(Notification.is_active.is_(True))
        .filter(
            Notification.status.in_(
                (NotificationStatus.queued, NotificationStatus.sending)
            )
        )
        .scalar()
        or 0
    )
    return queued + admin_alerts_service.count_unread_admin_notifications(db)


def _count_pending_location_requests(db: Session) -> int:
    """Count pending customer pin-correction requests for the GIS nav badge."""
    return (
        db.query(func.count(CustomerLocationChangeRequest.id))
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.pending
        )
        .scalar()
        or 0
    )


def get_sidebar_stats(db: Session) -> dict:
    """Get stats for sidebar badges."""
    global _sidebar_stats_cached_at, _sidebar_stats_cache

    from app.services import module_manager as module_manager_service

    now = monotonic()
    with _sidebar_stats_lock:
        if (
            _sidebar_stats_cache
            and (now - _sidebar_stats_cached_at) < _SIDEBAR_STATS_TTL_SECONDS
        ):
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
        pending_location_requests = _count_pending_location_requests(db)
    except Exception:
        pending_location_requests = 0

    resolved_brand = None
    try:
        from app.services.brand_profiles import resolve_brand

        resolved_brand = resolve_brand(db)
        sidebar_logo_url = resolved_brand.logo_url
        sidebar_logo_dark_url = resolved_brand.dark_logo_url
        favicon_url = resolved_brand.favicon_url
        app_name = resolved_brand.product_name
    except Exception:
        sidebar_logo_url = ""
        sidebar_logo_dark_url = ""
        favicon_url = ""
        app_name = ""
    try:
        module_states = module_manager_service.load_module_states(db)
        feature_states = module_manager_service.load_feature_states(db)
    except Exception:
        module_states = {}
        feature_states = {}

    stats = {
        "service_orders": service_orders_count,
        "notifications_unread": notifications_unread,
        "pending_location_requests": pending_location_requests,
        "sidebar_logo_url": sidebar_logo_url,
        "sidebar_logo_dark_url": sidebar_logo_dark_url,
        "favicon_url": favicon_url,
        "app_name": app_name,
        "brand": resolved_brand.to_dict() if resolved_brand else None,
        "module_states": module_states,
        "feature_states": feature_states,
    }
    with _sidebar_stats_lock:
        _sidebar_stats_cached_at = monotonic()
        _sidebar_stats_cache = dict(stats)

    return stats
