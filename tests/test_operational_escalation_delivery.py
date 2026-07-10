from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEntityType,
    OperationalEscalationStatus,
    OperationalNotificationChannel,
    OperationalParticipantType,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import operational_escalation, operational_escalation_delivery


def _team_with_member(db_session, *, email: str = "noc@example.com") -> ServiceTeam:
    team = ServiceTeam(name="NOC", team_type=ServiceTeamType.operations.value)
    user = SystemUser(first_name="Noc", last_name="Lead", email=email)
    db_session.add_all([team, user])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=user.id))
    db_session.flush()
    return team


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Field",
        last_name="Lead",
        email="field-lead@example.com",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _event(db_session):
    return operational_escalation.record_event(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=uuid4(),
        trigger="high_severity",
        level=2,
        metadata={
            "title": "OUTAGE ESCALATION: Garki POP",
            "body": "Owner update overdue",
        },
    )


def _adapter_result(success: bool = True, *, error: str | None = None):
    return SimpleNamespace(
        success=success,
        message="sent" if success else "failed",
        status=SimpleNamespace(value="sent" if success else "failed"),
        error=error,
    )


def test_dispatch_team_email_uses_notification_adapter(db_session, monkeypatch):
    team = _team_with_member(db_session, email="noc-lead@example.com")
    event = _event(db_session)
    delivery = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.email,
        recipient_type=OperationalParticipantType.team,
        recipient_id=team.id,
    )
    calls = []

    def fake_send_notification(channel, recipient, message, **kwargs):
        calls.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                **kwargs,
            }
        )
        return _adapter_result()

    monkeypatch.setattr(
        "app.services.notification_adapter.send_notification",
        fake_send_notification,
    )

    result = operational_escalation_delivery.dispatch_delivery(db_session, delivery)

    assert result.delivery_status == OperationalDeliveryStatus.sent
    assert result.sent_at is not None
    assert len(calls) == 1
    assert calls[0]["channel"] == OperationalNotificationChannel.email
    assert calls[0]["recipient"] == "noc-lead@example.com"
    assert calls[0]["message"] == "Owner update overdue"
    assert calls[0]["title"] == "OUTAGE ESCALATION: Garki POP"
    assert calls[0]["subject"] == "OUTAGE ESCALATION: Garki POP"
    assert calls[0]["idempotency_key"] == delivery.dedup_key
    assert calls[0]["metadata"]["delivery_id"] == str(delivery.id)


def test_dispatch_subscriber_whatsapp_uses_migrated_connector(
    db_session,
    monkeypatch,
):
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email="ada@example.com",
        phone="+2348000000000",
    )
    db_session.add(subscriber)
    db_session.flush()
    event = _event(db_session)
    delivery = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.whatsapp,
        recipient_type=OperationalParticipantType.subscriber,
        recipient_id=subscriber.id,
    )
    calls = []

    def fake_send_text_message(db, *, recipient, body, dry_run):
        calls.append({"recipient": recipient, "body": body, "dry_run": dry_run})
        return {"ok": True, "provider": "meta_cloud_api", "sent": True}

    monkeypatch.setattr(
        "app.services.integrations.connectors.whatsapp.send_text_message",
        fake_send_text_message,
    )

    result = operational_escalation_delivery.dispatch_delivery(db_session, delivery)

    assert result.delivery_status == OperationalDeliveryStatus.sent
    assert calls == [
        {
            "recipient": "+2348000000000",
            "body": "Owner update overdue",
            "dry_run": False,
        }
    ]
    assert result.metadata_["dispatch_results"][0]["provider"] == "meta_cloud_api"


def test_dispatch_staff_push_uses_fcm_service(db_session, monkeypatch):
    user = _system_user(db_session)
    event = _event(db_session)
    delivery = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.push,
        recipient_type=OperationalParticipantType.person,
        recipient_id=user.id,
    )
    calls = []

    def fake_send_push_to_system_user(
        db,
        system_user_id,
        title,
        body,
        *,
        data,
        notification_id,
    ):
        calls.append(
            {
                "system_user_id": system_user_id,
                "title": title,
                "body": body,
                "data": data,
                "notification_id": notification_id,
            }
        )
        return True

    monkeypatch.setattr(
        "app.services.push.send_push_to_system_user",
        fake_send_push_to_system_user,
    )

    result = operational_escalation_delivery.dispatch_delivery(db_session, delivery)

    assert result.delivery_status == OperationalDeliveryStatus.sent
    assert calls[0]["system_user_id"] == str(user.id)
    assert calls[0]["title"] == "OUTAGE ESCALATION: Garki POP"
    assert calls[0]["body"] == "Owner update overdue"
    assert calls[0]["notification_id"] == str(delivery.id)


def test_dispatch_pending_suppresses_closed_event(db_session, monkeypatch):
    team = _team_with_member(db_session)
    event = _event(db_session)
    event.status = OperationalEscalationStatus.canceled
    delivery = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.email,
        recipient_type=OperationalParticipantType.team,
        recipient_id=team.id,
    )

    def fake_send_notification(*args, **kwargs):
        raise AssertionError("closed events must not send")

    monkeypatch.setattr(
        "app.services.notification_adapter.send_notification",
        fake_send_notification,
    )

    [result] = operational_escalation_delivery.dispatch_pending_deliveries(db_session)

    assert result.id == delivery.id
    assert result.delivery_status == OperationalDeliveryStatus.suppressed
    assert result.error_message == "event.canceled"


def test_dispatch_marks_failed_when_no_target(db_session):
    event = _event(db_session)
    delivery = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.email,
        recipient_type=OperationalParticipantType.team,
        recipient_id=uuid4(),
    )

    result = operational_escalation_delivery.dispatch_delivery(db_session, delivery)

    assert result.delivery_status == OperationalDeliveryStatus.failed
    assert result.error_message == "No delivery target"
