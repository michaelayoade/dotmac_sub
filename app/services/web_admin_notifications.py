"""Service helpers for admin notifications dropdown."""

from uuid import UUID

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.services import web_admin as web_admin_service
from app.services.staff_notification_read_state import (
    StaffNotificationMenuItem,
    StaffNotificationMenuQuery,
    get_staff_notification_menu,
)

templates = Jinja2Templates(directory="templates")


def notifications_menu(request: Request, db: Session):
    current_user = web_admin_service.get_current_user(request)
    admin_notifications: tuple[StaffNotificationMenuItem, ...]
    system_user_id_value = (
        current_user.get("id")
        if current_user.get("principal_type") == "system_user"
        else None
    )
    try:
        system_user_id = UUID(str(system_user_id_value))
    except (TypeError, ValueError):
        system_user_id = None
    if system_user_id is None:
        admin_notifications = ()
        admin_unread_count = 0
    else:
        projection = get_staff_notification_menu(
            db,
            StaffNotificationMenuQuery(system_user_id=system_user_id),
        )
        admin_notifications = projection.items
        admin_unread_count = projection.unread_count
    response = templates.TemplateResponse(
        request,
        "admin/partials/notifications_menu.html",
        {
            "request": request,
            "admin_notifications": admin_notifications,
            "admin_unread_count": admin_unread_count,
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response
