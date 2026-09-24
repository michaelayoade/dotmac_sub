from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from email import message_from_string
from urllib.parse import urlencode
from uuid import uuid4

from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.services import email as email_service
from app.services import zeptomail_delivery_reconciliation as reconciliation
from app.services import zeptomail_delivery_transport as transport
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests.mocks import FakeSMTP


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="test:zeptomail",
        scope="notifications:delivery-reconciliation",
        reason="test provider status reconciliation",
        idempotency_key=key,
    )


def test_zeptomail_smtp_acceptance_is_submitted_not_delivered(db_session, monkeypatch):
    fake_smtp = FakeSMTP()
    monkeypatch.setattr(email_service, "_create_smtp_client", lambda *a, **k: fake_smtp)
    monkeypatch.setattr(
        email_service,
        "_get_smtp_config",
        lambda *a, **k: {
            "host": "smtp.zeptomail.com",
            "port": 587,
            "username": "emailapikey",
            "password": "test-password",
            "use_tls": True,
            "use_ssl": False,
            "from_email": "noc@dotmac.ng",
            "from_name": "Dotmac NOC",
            "sender_key": "noc",
        },
    )
    notification = Notification(
        channel=NotificationChannel.email,
        recipient="customer@example.com",
        subject="Delivery tracking",
        body="Test",
        status=NotificationStatus.sending,
    )
    db_session.add(notification)
    db_session.commit()

    assert email_service.send_email(
        db=db_session,
        to_email=notification.recipient,
        subject=notification.subject,
        body_html="<p>Test</p>",
        body_text="Test",
        track=False,
        notification_id=str(notification.id),
        sender_key="noc",
    )

    db_session.refresh(notification)
    assert notification.status is NotificationStatus.submitted
    parsed = message_from_string(fake_smtp.messages[0][2])
    assert parsed["X-TM-CLIENT-REF"] == str(notification.id)
    delivery = db_session.query(NotificationDelivery).one()
    assert delivery.status is DeliveryStatus.accepted
    assert notification.metadata_["delivery_provider"] == "zeptomail"


def test_provider_process_failure_replaces_submitted_status(db_session):
    notification = Notification(
        channel=NotificationChannel.email,
        recipient="customer@example.com",
        subject="Delivery tracking",
        body="Test",
        status=NotificationStatus.submitted,
        metadata_={"delivery_provider": "zeptomail"},
    )
    db_session.add(notification)
    db_session.commit()
    notification_id = notification.id
    db_session_adapter.release_read_transaction(db_session)
    observed_at = datetime.now(UTC)

    outcome = reconciliation.apply_zeptomail_delivery_status(
        db_session,
        reconciliation.ApplyZeptoMailDeliveryStatusCommand(
            context=_context("process-failed"),
            notification_id=notification_id,
            provider_status="Process failed",
            observed_at=observed_at,
            email_reference="ref-process-failed",
            request_id="request-process-failed",
            reason="relaying-issues — Mail sending blocked",
        ),
    )

    assert outcome.kind == "updated"
    db_session.refresh(notification)
    assert notification.status is NotificationStatus.failed
    assert notification.last_error == "relaying-issues — Mail sending blocked"
    delivery = db_session.query(NotificationDelivery).one()
    assert delivery.status is DeliveryStatus.failed
    assert delivery.provider_message_id == "ref-process-failed"


def test_provider_delivered_status_is_final(db_session):
    notification = Notification(
        channel=NotificationChannel.email,
        recipient="customer@example.com",
        subject="Delivery tracking",
        body="Test",
        status=NotificationStatus.submitted,
    )
    db_session.add(notification)
    db_session.commit()
    notification_id = notification.id
    db_session_adapter.release_read_transaction(db_session)
    observed_at = datetime.now(UTC)

    reconciliation.apply_zeptomail_delivery_status(
        db_session,
        reconciliation.ApplyZeptoMailDeliveryStatusCommand(
            context=_context("delivered"),
            notification_id=notification_id,
            provider_status="Delivered",
            observed_at=observed_at,
            email_reference="ref-delivered",
            request_id="request-delivered",
        ),
    )

    db_session.refresh(notification)
    assert notification.status is NotificationStatus.delivered
    assert notification.sent_at is not None
    assert notification.sent_at.replace(tzinfo=UTC) == observed_at


def test_signed_webhook_uses_client_reference_to_find_notification():
    notification_id = uuid4()
    now = datetime.now(UTC)
    event = {
        "event_name": "hardbounce",
        "event_message": {
            "email_info": {
                "client_reference": str(notification_id),
                "email_reference": "ref-bounced",
            },
            "event_data": {
                "details": {
                    "time": now.isoformat(),
                    "reason": "mailbox unavailable",
                }
            },
            "request_id": "request-bounced",
        },
    }
    event_json = json.dumps(event, separators=(",", ":"))
    secret = "test-webhook-secret"
    signature = base64.b64encode(
        hmac.new(secret.encode(), event_json.encode(), hashlib.sha256).digest()
    ).decode()
    header = f"ts={int(now.timestamp() * 1000)};s={signature};s-algorithm=HmacSHA256"

    fact = transport.parse_signed_webhook(
        raw_body=urlencode({"eventData": event_json}).encode(),
        producer_signature=header,
        authentication_key=secret,
        now=now,
    )

    assert fact.notification_id == notification_id
    assert fact.provider_status == "hardbounce"
    assert fact.reason == "mailbox unavailable"
