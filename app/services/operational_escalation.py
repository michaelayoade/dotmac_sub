from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationPolicy,
    OperationalEscalationStatus,
    OperationalOwner,
    OperationalOwnerRole,
    OperationalParticipantType,
    OperationalRoomLink,
    OperationalWatcher,
    OperationalWatcherRole,
)


def _entity_id(value: str | UUID) -> str:
    return str(value)


def _participant_type(
    *,
    service_team_id: str | UUID | None = None,
    person_id: str | UUID | None = None,
    duty_role: str | None = None,
    external: bool = False,
) -> str:
    selected = [
        service_team_id is not None,
        person_id is not None,
        bool(duty_role),
        external,
    ]
    if sum(1 for item in selected if item) != 1:
        raise ValueError("exactly one participant target is required")
    if service_team_id is not None:
        return OperationalParticipantType.team
    if person_id is not None:
        return OperationalParticipantType.person
    if duty_role:
        return OperationalParticipantType.duty_role
    return OperationalParticipantType.external


def _uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def set_owner(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    service_team_id: str | UUID | None = None,
    person_id: str | UUID | None = None,
    duty_role: str | None = None,
    role: str = OperationalOwnerRole.primary,
    source: str | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
    assigned_at: datetime | None = None,
) -> OperationalOwner:
    owner_type = _participant_type(
        service_team_id=service_team_id,
        person_id=person_id,
        duty_role=duty_role,
    )
    normalized_entity_id = _entity_id(entity_id)
    if role == OperationalOwnerRole.primary:
        (
            db.query(OperationalOwner)
            .filter(OperationalOwner.entity_type == entity_type)
            .filter(OperationalOwner.entity_id == normalized_entity_id)
            .filter(OperationalOwner.role == OperationalOwnerRole.primary)
            .filter(OperationalOwner.is_active.is_(True))
            .update({"is_active": False}, synchronize_session="fetch")
        )

    owner = OperationalOwner(
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        owner_type=owner_type,
        role=role,
        service_team_id=_uuid(service_team_id),
        person_id=_uuid(person_id),
        duty_role=duty_role,
        source=source,
        reason=reason,
        metadata_=metadata,
        assigned_at=assigned_at or datetime.now(UTC),
    )
    db.add(owner)
    db.flush()
    return owner


def add_watcher(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    service_team_id: str | UUID | None = None,
    person_id: str | UUID | None = None,
    duty_role: str | None = None,
    external: bool = False,
    role: str = OperationalWatcherRole.watcher,
    source: str | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> OperationalWatcher:
    watcher_type = _participant_type(
        service_team_id=service_team_id,
        person_id=person_id,
        duty_role=duty_role,
        external=external,
    )
    normalized_entity_id = _entity_id(entity_id)
    existing = (
        db.query(OperationalWatcher)
        .filter(OperationalWatcher.entity_type == entity_type)
        .filter(OperationalWatcher.entity_id == normalized_entity_id)
        .filter(OperationalWatcher.watcher_type == watcher_type)
        .filter(OperationalWatcher.service_team_id == _uuid(service_team_id))
        .filter(OperationalWatcher.person_id == _uuid(person_id))
        .filter(OperationalWatcher.duty_role == duty_role)
        .one_or_none()
    )
    if existing is not None:
        existing.role = role
        existing.source = source or existing.source
        existing.reason = reason or existing.reason
        existing.metadata_ = metadata or existing.metadata_
        existing.is_active = True
        db.flush()
        return existing

    watcher = OperationalWatcher(
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        watcher_type=watcher_type,
        role=role,
        service_team_id=_uuid(service_team_id),
        person_id=_uuid(person_id),
        duty_role=duty_role,
        source=source,
        reason=reason,
        metadata_=metadata,
    )
    db.add(watcher)
    db.flush()
    return watcher


def list_watchers(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    active_only: bool = True,
) -> list[OperationalWatcher]:
    query = (
        db.query(OperationalWatcher)
        .filter(OperationalWatcher.entity_type == entity_type)
        .filter(OperationalWatcher.entity_id == _entity_id(entity_id))
        .order_by(OperationalWatcher.created_at.asc())
    )
    if active_only:
        query = query.filter(OperationalWatcher.is_active.is_(True))
    return list(query.all())


def link_room(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    provider: str,
    room_id: str,
    room_name: str | None = None,
    room_url: str | None = None,
    metadata: dict | None = None,
) -> OperationalRoomLink:
    normalized_entity_id = _entity_id(entity_id)
    existing = (
        db.query(OperationalRoomLink)
        .filter(OperationalRoomLink.entity_type == entity_type)
        .filter(OperationalRoomLink.entity_id == normalized_entity_id)
        .filter(OperationalRoomLink.provider == provider)
        .filter(OperationalRoomLink.room_id == room_id)
        .one_or_none()
    )
    if existing is not None:
        existing.room_name = room_name or existing.room_name
        existing.room_url = room_url or existing.room_url
        existing.metadata_ = metadata or existing.metadata_
        existing.is_active = True
        db.flush()
        return existing
    link = OperationalRoomLink(
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        provider=provider,
        room_id=room_id,
        room_name=room_name,
        room_url=room_url,
        metadata_=metadata,
    )
    db.add(link)
    db.flush()
    return link


def create_policy(
    db: Session,
    *,
    name: str,
    entity_type: str | None = None,
    level: int = 1,
    channels: list[str] | None = None,
    cooldown_seconds: int = 1800,
    scope_type: str | None = None,
    scope_id: str | None = None,
    min_severity: str | None = None,
    min_affected_customers: int | None = None,
    vip_only: bool = False,
    unowned_after_seconds: int | None = None,
    stale_owner_update_seconds: int | None = None,
    customer_update_due_within_seconds: int | None = None,
    unresolved_after_seconds: int | None = None,
    metadata: dict | None = None,
) -> OperationalEscalationPolicy:
    policy = OperationalEscalationPolicy(
        name=name,
        entity_type=entity_type,
        scope_type=scope_type,
        scope_id=scope_id,
        level=level,
        min_severity=min_severity,
        min_affected_customers=min_affected_customers,
        vip_only=vip_only,
        unowned_after_seconds=unowned_after_seconds,
        stale_owner_update_seconds=stale_owner_update_seconds,
        customer_update_due_within_seconds=customer_update_due_within_seconds,
        unresolved_after_seconds=unresolved_after_seconds,
        channels=channels or [],
        cooldown_seconds=cooldown_seconds,
        metadata_=metadata,
    )
    db.add(policy)
    db.flush()
    return policy


def record_event(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    trigger: str,
    level: int = 1,
    policy_id: str | UUID | None = None,
    severity: str | None = None,
    affected_customer_count: int | None = None,
    metadata: dict | None = None,
    triggered_at: datetime | None = None,
) -> OperationalEscalationEvent:
    event = OperationalEscalationEvent(
        entity_type=entity_type,
        entity_id=_entity_id(entity_id),
        policy_id=_uuid(policy_id),
        level=level,
        trigger=trigger,
        severity=severity,
        affected_customer_count=affected_customer_count,
        metadata_=metadata,
        triggered_at=triggered_at or datetime.now(UTC),
    )
    db.add(event)
    db.flush()
    return event


def plan_delivery(
    db: Session,
    *,
    event: OperationalEscalationEvent,
    channel: str,
    recipient_type: str,
    recipient_id: str | UUID | None = None,
    recipient_address: str | None = None,
    watcher_id: str | UUID | None = None,
    owner_id: str | UUID | None = None,
    cooldown_seconds: int = 1800,
    metadata: dict | None = None,
) -> OperationalEscalationDelivery:
    normalized_recipient_id = _entity_id(recipient_id) if recipient_id else None
    dedup_key = ":".join(
        [
            event.entity_type,
            event.entity_id,
            str(event.level),
            channel,
            recipient_type,
            normalized_recipient_id or recipient_address or "unknown",
        ]
    )
    existing = (
        db.query(OperationalEscalationDelivery)
        .filter(OperationalEscalationDelivery.dedup_key == dedup_key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    delivery = OperationalEscalationDelivery(
        event_id=event.id,
        watcher_id=_uuid(watcher_id),
        owner_id=_uuid(owner_id),
        channel=channel,
        recipient_type=recipient_type,
        recipient_id=normalized_recipient_id,
        recipient_address=recipient_address,
        delivery_status=OperationalDeliveryStatus.pending,
        dedup_key=dedup_key,
        escalation_level=event.level,
        cooldown_until=now + timedelta(seconds=cooldown_seconds),
        metadata_=metadata,
    )
    db.add(delivery)
    db.flush()
    return delivery


def mark_delivery_sent(
    db: Session,
    delivery: OperationalEscalationDelivery,
    *,
    sent_at: datetime | None = None,
) -> OperationalEscalationDelivery:
    delivery.delivery_status = OperationalDeliveryStatus.sent
    delivery.sent_at = sent_at or datetime.now(UTC)
    db.flush()
    return delivery


def acknowledge_event(
    db: Session,
    event: OperationalEscalationEvent,
    *,
    person_id: str | UUID | None = None,
    acknowledged_at: datetime | None = None,
) -> OperationalEscalationEvent:
    now = acknowledged_at or datetime.now(UTC)
    event.status = OperationalEscalationStatus.acknowledged
    event.acknowledged_by_person_id = _uuid(person_id)
    event.acknowledged_at = now
    for delivery in event.deliveries:
        if delivery.delivery_status in {
            OperationalDeliveryStatus.pending,
            OperationalDeliveryStatus.sent,
        }:
            delivery.delivery_status = OperationalDeliveryStatus.acknowledged
            delivery.acknowledged_at = now
    db.flush()
    return event
