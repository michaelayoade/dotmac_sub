"""Service helpers for admin notifications dropdown."""

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.services import notification as notification_service
from app.services import web_admin as web_admin_service

templates = Jinja2Templates(directory="templates")


def notifications_menu(request: Request, db: Session):
    current_user = web_admin_service.get_current_user(request)
    recipients = {
        current_user.get("email"),
        current_user.get("subscriber_id"),
        current_user.get("id"),
    }
    recipients.discard(None)
    recipients.discard("")

    if recipients:
        notifications = (
            db.query(Notification)
            .filter(Notification.is_active.is_(True))
            .filter(Notification.recipient.in_(list(recipients)))
            .order_by(Notification.created_at.desc())
            .limit(10)
            .all()
        )
    else:
        notifications = notification_service.notifications.list(
            db=db,
            channel=None,
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    return templates.TemplateResponse(
        "admin/partials/notifications_menu.html",
        {"request": request, "notifications": notifications},
    )
