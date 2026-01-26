"""Send an in-app notification to all admin users."""

import argparse
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.db import SessionLocal
from app.models.notification import NotificationChannel, NotificationStatus
from app.models.rbac import Role, PersonRole
from app.schemas.notification import NotificationBulkCreateRequest
from app.services import notification as notification_service


def parse_args():
    parser = argparse.ArgumentParser(description="Send in-app notification to all admin users.")
    parser.add_argument("--subject", default="Admin notification", help="Notification subject")
    parser.add_argument("--body", default="Please review the latest update.", help="Notification body")
    parser.add_argument(
        "--status",
        default="delivered",
        choices=["queued", "delivered"],
        help="Notification status",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    status = NotificationStatus.delivered if args.status == "delivered" else NotificationStatus.queued

    with SessionLocal() as db:
        role = db.query(Role).filter(Role.name == "admin").first()
        if not role:
            print("No admin role found.")
            return

        person_ids = [
            str(row.person_id)
            for row in db.query(PersonRole.person_id)
            .filter(PersonRole.role_id == role.id)
            .distinct()
            .all()
        ]
        if not person_ids:
            print("No admin users found.")
            return

        payload = NotificationBulkCreateRequest(
            channel=NotificationChannel.push,
            recipients=person_ids,
            subject=args.subject,
            body=args.body,
            status=status,
        )
        response = notification_service.notifications.bulk_create_response(db, payload)
        print(f"Sent {response.get('created', 0)} notifications.")


if __name__ == "__main__":
    main()
