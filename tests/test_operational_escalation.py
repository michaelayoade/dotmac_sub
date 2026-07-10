from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEntityType,
    OperationalEscalationStatus,
    OperationalNotificationChannel,
    OperationalOwner,
    OperationalParticipantType,
    OperationalRoomProvider,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber
from app.services import operational_escalation


def _team(db_session, name: str = "NOC") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.operations.value)
    db_session.add(team)
    db_session.flush()
    return team


def test_set_owner_replaces_active_primary_owner(db_session):
    first = _team(db_session, "NOC")
    second = _team(db_session, "Field")
    incident_id = uuid4()

    old_owner = operational_escalation.set_owner(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
        service_team_id=first.id,
        source="topology_rule",
    )
    new_owner = operational_escalation.set_owner(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
        service_team_id=second.id,
        source="manual",
        reason="Escalated to field lead",
    )
    db_session.commit()

    db_session.refresh(old_owner)
    active = (
        db_session.query(OperationalOwner)
        .filter(OperationalOwner.entity_type == OperationalEntityType.outage)
        .filter(OperationalOwner.entity_id == str(incident_id))
        .filter(OperationalOwner.is_active.is_(True))
        .all()
    )
    assert old_owner.is_active is False
    assert active == [new_owner]
    assert new_owner.service_team_id == second.id


def test_add_watcher_is_idempotent_and_reactivates(db_session):
    team = _team(db_session)
    incident_id = uuid4()

    watcher = operational_escalation.add_watcher(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
        service_team_id=team.id,
        source="site_owner",
    )
    watcher.is_active = False
    same = operational_escalation.add_watcher(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
        service_team_id=team.id,
        source="site_owner",
    )

    assert same.id == watcher.id
    assert same.is_active is True
    assert same.watcher_type == OperationalParticipantType.team
    assert operational_escalation.list_watchers(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
    ) == [same]


def test_watcher_requires_exactly_one_target(db_session):
    team = _team(db_session)
    with pytest.raises(ValueError):
        operational_escalation.add_watcher(
            db_session,
            entity_type=OperationalEntityType.outage,
            entity_id=uuid4(),
            service_team_id=team.id,
            person_id=uuid4(),
        )


def test_customer_can_be_watcher(db_session):
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email="ada-watcher@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()

    watcher = operational_escalation.add_watcher(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=uuid4(),
        subscriber_id=subscriber.id,
        source="affected_customer",
        reason="VIP customer affected",
    )

    assert watcher.watcher_type == OperationalParticipantType.subscriber
    assert watcher.subscriber_id == subscriber.id


def test_link_room_is_idempotent(db_session):
    incident_id = uuid4()

    first = operational_escalation.link_room(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
        provider=OperationalRoomProvider.nextcloud_talk,
        room_id="OUTAGE-GARKI-OLT",
        room_name="OUTAGE-GARKI-OLT",
    )
    second = operational_escalation.link_room(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
        provider=OperationalRoomProvider.nextcloud_talk,
        room_id="OUTAGE-GARKI-OLT",
        room_url="https://talk.example/room",
    )

    assert second.id == first.id
    assert second.room_name == "OUTAGE-GARKI-OLT"
    assert second.room_url == "https://talk.example/room"


def test_plan_delivery_dedupes_by_incident_level_channel_and_recipient(db_session):
    incident_id = uuid4()
    policy = operational_escalation.create_policy(
        db_session,
        name="High outage WhatsApp",
        entity_type=OperationalEntityType.outage,
        level=2,
        channels=["whatsapp"],
        min_affected_customers=100,
    )
    event = operational_escalation.record_event(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident_id,
        policy_id=policy.id,
        trigger="affected_customer_threshold",
        level=2,
        severity="high",
        affected_customer_count=184,
    )

    first = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.whatsapp,
        recipient_type=OperationalParticipantType.person,
        recipient_id=uuid4(),
        recipient_address="+2348000000000",
        cooldown_seconds=900,
    )
    duplicate = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.whatsapp,
        recipient_type=OperationalParticipantType.person,
        recipient_id=first.recipient_id,
        recipient_address="+2348000000000",
        cooldown_seconds=900,
    )

    assert duplicate.id == first.id
    assert first.delivery_status == OperationalDeliveryStatus.pending
    assert first.cooldown_until is not None
    assert first.escalation_level == 2


def test_acknowledge_event_marks_pending_deliveries_acknowledged(db_session):
    event = operational_escalation.record_event(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=uuid4(),
        trigger="unowned_incident",
        level=2,
    )
    delivery = operational_escalation.plan_delivery(
        db_session,
        event=event,
        channel=OperationalNotificationChannel.email,
        recipient_type=OperationalParticipantType.duty_role,
        recipient_id="noc_lead",
    )

    operational_escalation.acknowledge_event(
        db_session,
        event,
        person_id=uuid4(),
    )

    assert event.status == OperationalEscalationStatus.acknowledged
    assert event.acknowledged_at is not None
    assert delivery.delivery_status == OperationalDeliveryStatus.acknowledged
    assert delivery.acknowledged_at == event.acknowledged_at
