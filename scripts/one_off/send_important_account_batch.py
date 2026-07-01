"""One-off sender for the 2026-06-25 Important Account bulk email batch."""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import select

from app.db import SessionLocal
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    NotificationStatus,
)
from app.services.email import _create_smtp_client, get_smtp_config
from app.services.email_template import html_to_text, render_email_bodies

BATCH_CREATED_AT = datetime.fromisoformat("2026-06-25T13:28:00+00:00")
TEMPLATE_CODE = "important_account_information"
MAX_PROCESSED = 100


def _connect(config):
    server = _create_smtp_client(
        config["host"],
        config["port"],
        bool(config.get("use_ssl")),
    )
    if config.get("use_tls") and not config.get("use_ssl"):
        server.starttls()
    if config.get("username") and config.get("password"):
        server.login(config["username"], config["password"])
    return server


def _message(config, notification):
    subject = notification.subject or "Notification"
    body_html, body_text = render_email_bodies(notification.body or "", subject=subject)
    if body_text is None:
        body_text = html_to_text(body_html)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config['from_name']} <{config['from_email']}>"
    msg["To"] = notification.recipient
    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    return msg


def main() -> None:
    db = SessionLocal()
    config = get_smtp_config(db, activity="notification_queue")
    provider_name = f"smtp:{config.get('sender_key', 'default')}"
    server = _connect(config)
    delivered = 0
    failed = 0
    processed = 0

    try:
        while True:
            notification = db.scalars(
                select(Notification)
                .join(Notification.template)
                .where(Notification.template.has(code=TEMPLATE_CODE))
                .where(Notification.event_type == "service_bulk_message")
                .where(Notification.created_at >= BATCH_CREATED_AT)
                .where(Notification.status == NotificationStatus.queued)
                .order_by(Notification.created_at.asc(), Notification.id.asc())
                .limit(1)
            ).first()
            if notification is None:
                break

            notification.status = NotificationStatus.sending
            notification.last_error = None
            notification.updated_at = datetime.now(UTC)
            db.commit()

            try:
                msg = _message(config, notification)
                server.sendmail(
                    config["from_email"],
                    notification.recipient,
                    msg.as_string(),
                )
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError):
                try:
                    server.quit()
                except Exception:
                    pass
                server = _connect(config)
                msg = _message(config, notification)
                server.sendmail(
                    config["from_email"],
                    notification.recipient,
                    msg.as_string(),
                )
            except Exception as exc:
                notification.status = NotificationStatus.failed
                notification.last_error = str(exc)
                notification.retry_count = (notification.retry_count or 0) + 1
                db.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        provider=provider_name,
                        provider_message_id=None,
                        status=DeliveryStatus.failed,
                        response_code="smtp_send_failed",
                        response_body=str(exc),
                    )
                )
                db.commit()
                failed += 1
            else:
                notification.status = NotificationStatus.delivered
                notification.last_error = None
                notification.sent_at = datetime.now(UTC)
                notification.updated_at = datetime.now(UTC)
                db.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        provider=provider_name,
                        provider_message_id=None,
                        status=DeliveryStatus.delivered,
                        response_code="sent",
                        response_body="SMTP send completed",
                    )
                )
                db.commit()
                delivered += 1

            processed += 1
            if processed % 100 == 0:
                print(
                    {
                        "processed": processed,
                        "delivered": delivered,
                        "failed": failed,
                    },
                    flush=True,
                )
            if processed >= MAX_PROCESSED:
                break
    finally:
        try:
            server.quit()
        except Exception:
            pass
        db.close()

    print(
        {
            "processed": processed,
            "delivered": delivered,
            "failed": failed,
            "done": True,
        },
        flush=True,
    )


if __name__ == "__main__":
    main()
