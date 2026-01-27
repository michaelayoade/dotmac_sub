"""Service helpers for admin web layer."""

from sqlalchemy.orm import Session


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
        return {
            "id": str(getattr(user, "id", "")),
            "person_id": str(person_id if person_id else getattr(user, "id", "")),
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
    from app.services import provisioning as provisioning_service

    def get_status(obj):
        status = getattr(obj, "status", "")
        return status.value if hasattr(status, "value") else str(status)

    try:
        orders = provisioning_service.service_orders.list(
            db=db,
            account_id=None,
            subscription_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        service_orders_count = sum(
            1 for o in orders
            if get_status(o) not in ("completed", "cancelled", "canceled")
        )
    except Exception:
        service_orders_count = 0

    return {
        "service_orders": service_orders_count,
        "dispatch_jobs": 0,
    }
