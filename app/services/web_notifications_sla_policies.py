"""Admin form helpers for operational SLA escalation policies."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.operational_escalation import (
    OperationalEscalationPolicy,
    OperationalNotificationChannel,
)
from app.services import operational_escalation

CHANNELS = (
    OperationalNotificationChannel.email,
    OperationalNotificationChannel.whatsapp,
    OperationalNotificationChannel.sms,
    OperationalNotificationChannel.push,
    OperationalNotificationChannel.web,
    OperationalNotificationChannel.nextcloud_talk,
    OperationalNotificationChannel.webhook,
)
SEVERITIES = ("info", "low", "warning", "high", "critical")


def list_data(
    db: Session,
    *,
    trigger: str | None,
    active: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    query = db.query(OperationalEscalationPolicy)
    if trigger:
        query = query.filter(OperationalEscalationPolicy.trigger == trigger)
    active_filter = str(active or "").strip().lower()
    if active_filter == "active":
        query = query.filter(OperationalEscalationPolicy.is_active.is_(True))
    elif active_filter == "inactive":
        query = query.filter(OperationalEscalationPolicy.is_active.is_(False))
    total = query.count()
    policies = (
        query.order_by(
            OperationalEscalationPolicy.trigger.asc(),
            OperationalEscalationPolicy.level.asc(),
            OperationalEscalationPolicy.created_at.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "policies": policies,
        "event_definitions": operational_escalation.KNOWN_SLA_EVENT_DEFINITIONS,
        "event_labels": {
            item.trigger: item.label
            for item in operational_escalation.KNOWN_SLA_EVENT_DEFINITIONS
        },
        "entity_types": operational_escalation.OPERATIONAL_ENTITY_TYPES,
        "trigger": trigger or "",
        "active": active_filter,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


def form_data(
    db: Session,
    *,
    policy_id: UUID | None = None,
) -> dict[str, object] | None:
    policy = db.get(OperationalEscalationPolicy, policy_id) if policy_id else None
    if policy_id and policy is None:
        return None
    return {
        "policy": policy,
        "event_definitions": operational_escalation.KNOWN_SLA_EVENT_DEFINITIONS,
        "entity_types": operational_escalation.OPERATIONAL_ENTITY_TYPES,
        "channels": CHANNELS,
        "severities": SEVERITIES,
        "selected_channels": set(policy.channels or []) if policy else set(),
        "delay_minutes": (
            int((policy.unresolved_after_seconds or 0) / 60) if policy else 0
        ),
        "notes": str((policy.metadata_ or {}).get("notes") or "") if policy else "",
    }


def create_policy(
    db: Session,
    *,
    name: str,
    entity_type: str,
    trigger: str,
    level: int,
    delay_minutes: int,
    channels: list[str],
    min_severity: str | None,
    min_affected_customers: int | None,
    notes: str | None,
    is_active: bool,
) -> OperationalEscalationPolicy:
    values = _validated_values(
        db,
        name=name,
        entity_type=entity_type,
        trigger=trigger,
        level=level,
        delay_minutes=delay_minutes,
        channels=channels,
        min_severity=min_severity,
        min_affected_customers=min_affected_customers,
        exclude_policy_id=None,
        is_active=is_active,
    )
    policy = operational_escalation.create_policy(
        db,
        **values,
        cooldown_seconds=0,
        metadata={"notes": notes.strip()} if notes and notes.strip() else {},
    )
    return operational_escalation.commit_policy(db, policy, is_active=is_active)


def update_policy(
    db: Session,
    *,
    policy_id: UUID,
    name: str,
    entity_type: str,
    trigger: str,
    level: int,
    delay_minutes: int,
    channels: list[str],
    min_severity: str | None,
    min_affected_customers: int | None,
    notes: str | None,
    is_active: bool,
) -> OperationalEscalationPolicy:
    policy = db.get(OperationalEscalationPolicy, policy_id)
    if policy is None:
        raise ValueError("SLA policy not found")
    values = _validated_values(
        db,
        name=name,
        entity_type=entity_type,
        trigger=trigger,
        level=level,
        delay_minutes=delay_minutes,
        channels=channels,
        min_severity=min_severity,
        min_affected_customers=min_affected_customers,
        exclude_policy_id=policy_id,
        is_active=is_active,
    )
    operational_escalation.update_policy(
        db,
        policy,
        **values,
        metadata={"notes": notes.strip()} if notes and notes.strip() else {},
        is_active=is_active,
    )
    return operational_escalation.commit_policy(db, policy)


def deactivate_policy(db: Session, *, policy_id: UUID) -> None:
    policy = db.get(OperationalEscalationPolicy, policy_id)
    if policy is None:
        raise ValueError("SLA policy not found")
    operational_escalation.deactivate_policy_committed(db, policy)


def _validated_values(
    db: Session,
    *,
    name: str,
    entity_type: str,
    trigger: str,
    level: int,
    delay_minutes: int,
    channels: list[str],
    min_severity: str | None,
    min_affected_customers: int | None,
    exclude_policy_id: UUID | None,
    is_active: bool,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Policy name is required")
    normalized_entity_type, normalized_trigger = (
        operational_escalation.validate_sla_event(
            entity_type=entity_type,
            trigger=trigger,
        )
    )
    if level < 1:
        raise ValueError("Escalation level must be at least 1")
    if delay_minutes < 0 or delay_minutes > 525_600:
        raise ValueError("Escalation delay must be between 0 and 525600 minutes")
    selected_channels = list(dict.fromkeys(channels))
    invalid_channels = set(selected_channels) - set(CHANNELS)
    if invalid_channels:
        raise ValueError(
            f"Unsupported notification channels: {sorted(invalid_channels)}"
        )
    if not selected_channels:
        raise ValueError("Select at least one notification channel")
    if min_affected_customers is not None and min_affected_customers < 0:
        raise ValueError("Minimum affected customers cannot be negative")
    normalized_min_severity = min_severity.strip().lower() if min_severity else None
    if normalized_min_severity and normalized_min_severity not in SEVERITIES:
        raise ValueError("Select a supported minimum severity")
    if is_active:
        duplicate = (
            db.query(OperationalEscalationPolicy)
            .filter(OperationalEscalationPolicy.entity_type == normalized_entity_type)
            .filter(OperationalEscalationPolicy.trigger == normalized_trigger)
            .filter(OperationalEscalationPolicy.level == level)
            .filter(OperationalEscalationPolicy.is_active.is_(True))
        )
        if exclude_policy_id is not None:
            duplicate = duplicate.filter(
                OperationalEscalationPolicy.id != exclude_policy_id
            )
        if duplicate.first() is not None:
            raise ValueError(
                "An active policy already owns this event and escalation level"
            )
    return {
        "name": clean_name,
        "entity_type": normalized_entity_type,
        "trigger": normalized_trigger,
        "level": level,
        "channels": selected_channels,
        "min_severity": normalized_min_severity,
        "min_affected_customers": min_affected_customers,
        "unresolved_after_seconds": delay_minutes * 60,
    }
