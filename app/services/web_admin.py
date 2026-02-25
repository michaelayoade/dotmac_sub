"""Service helpers for admin web layer."""

from sqlalchemy.orm import Session

from app.models.provisioning import ServiceOrderStatus


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


def get_sidebar_stats(db: Session) -> dict:
    """Get stats for sidebar badges."""
    from app.models.domain_settings import SettingDomain
    from app.services import module_manager as module_manager_service
    from app.services import provisioning as provisioning_service
    from app.services import settings_spec

    try:
        orders = provisioning_service.service_orders.list(
            db=db,
            subscriber_id=None,
            subscription_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        service_orders_count = sum(
            1 for o in orders
            if o.status not in (ServiceOrderStatus.active, ServiceOrderStatus.canceled, ServiceOrderStatus.failed)
        )
    except Exception:
        service_orders_count = 0

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
        module_states = module_manager_service.load_module_states(db)
        feature_states = module_manager_service.load_feature_states(db)
    except Exception:
        module_states = {}
        feature_states = {}

    return {
        "service_orders": service_orders_count,
        "dispatch_jobs": 0,
        "sidebar_logo_url": sidebar_logo_url,
        "sidebar_logo_dark_url": sidebar_logo_dark_url,
        "favicon_url": favicon_url,
        "module_states": module_states,
        "feature_states": feature_states,
    }
