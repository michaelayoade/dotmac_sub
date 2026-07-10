from __future__ import annotations

from sqlalchemy import or_

from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
from app.models.operational_escalation import (
    OperationalEntityType,
    OperationalEscalationEvent,
    OperationalEscalationPolicy,
    OperationalEscalationStatus,
    OperationalOwner,
    OperationalOwnerRole,
    OperationalRoomProvider,
    OperationalWatcherRole,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.services import operational_escalation

SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "minor": 1,
    "medium": 2,
    "moderate": 2,
    "high": 3,
    "major": 3,
    "critical": 4,
}


def _scope_label(incident: OutageIncident) -> str:
    if incident.root_node_id is not None:
        return f"NODE-{incident.root_node_id}"
    if incident.basestation_id is not None:
        return f"SITE-{incident.basestation_id}"
    if incident.fdh_cabinet_id is not None:
        return f"FDH-{incident.fdh_cabinet_id}"
    return str(incident.id)


def _scope_name(session, incident: OutageIncident) -> str:
    if incident.root_node_id is not None:
        node = session.get(NetworkDevice, incident.root_node_id)
        if node is not None and node.name:
            return node.name
    if incident.basestation_id is not None:
        site = session.get(PopSite, incident.basestation_id)
        if site is not None and site.name:
            return site.name
    if incident.fdh_cabinet_id is not None:
        return f"FDH {incident.fdh_cabinet_id}"
    return str(incident.id)


def _active_team_by_type(session, team_type: str) -> ServiceTeam | None:
    return (
        session.query(ServiceTeam)
        .filter(ServiceTeam.team_type == team_type)
        .filter(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.created_at.asc())
        .first()
    )


def _has_active_primary_owner(session, incident: OutageIncident) -> bool:
    return (
        session.query(OperationalOwner)
        .filter(OperationalOwner.entity_type == OperationalEntityType.outage)
        .filter(OperationalOwner.entity_id == str(incident.id))
        .filter(OperationalOwner.role == OperationalOwnerRole.primary)
        .filter(OperationalOwner.is_active.is_(True))
        .first()
        is not None
    )


def ensure_outage_operations(session, incident: OutageIncident) -> None:
    """Attach generic operational owner/watchers/room metadata to an outage.

    This does not send notifications. It only records who owns the incident, who
    should watch it, and the deterministic Nextcloud room identity later delivery
    slices can create/link.
    """

    entity_id = str(incident.id)
    operations = _active_team_by_type(session, ServiceTeamType.operations.value)
    support = _active_team_by_type(session, ServiceTeamType.support.value)
    field = _active_team_by_type(session, ServiceTeamType.field_service.value)

    if operations is not None and not _has_active_primary_owner(session, incident):
        operational_escalation.set_owner(
            session,
            entity_type=OperationalEntityType.outage,
            entity_id=entity_id,
            service_team_id=operations.id,
            source="outage_lifecycle",
            reason="Default outage owner",
            metadata={
                "status": incident.status,
                "severity": incident.severity,
                "affected_count": incident.affected_count,
            },
        )

    for team, role in (
        (operations, OperationalWatcherRole.lead),
        (support, OperationalWatcherRole.watcher),
        (field, OperationalWatcherRole.watcher),
    ):
        if team is None:
            continue
        operational_escalation.add_watcher(
            session,
            entity_type=OperationalEntityType.outage,
            entity_id=entity_id,
            service_team_id=team.id,
            role=role,
            source="outage_lifecycle",
            reason="Outage coordination",
            metadata={
                "status": incident.status,
                "severity": incident.severity,
                "affected_count": incident.affected_count,
            },
        )

    room_name = f"OUTAGE-{_scope_name(session, incident)}-{str(incident.id)[:8]}"
    operational_escalation.link_room(
        session,
        entity_type=OperationalEntityType.outage,
        entity_id=entity_id,
        provider=OperationalRoomProvider.nextcloud_talk,
        room_id=room_name.upper().replace(" ", "-"),
        room_name=room_name,
        metadata={
            "provisioning_status": "planned",
            "scope": _scope_label(incident),
            "status": incident.status,
            "severity": incident.severity,
            "affected_count": incident.affected_count,
        },
    )


def plan_outage_escalations(
    session,
    incident: OutageIncident,
    *,
    trigger: str,
) -> list[OperationalEscalationEvent]:
    """Create escalation events and pending deliveries for matching outage policies."""

    events: list[OperationalEscalationEvent] = []
    for policy in _matching_outage_policies(session, incident):
        event = _record_outage_event_once(
            session,
            incident,
            policy=policy,
            trigger=trigger,
        )
        operational_escalation.plan_policy_deliveries(
            session,
            event=event,
            policy=policy,
        )
        events.append(event)
    return events


def _matching_outage_policies(
    session,
    incident: OutageIncident,
) -> list[OperationalEscalationPolicy]:
    policies = (
        session.query(OperationalEscalationPolicy)
        .filter(OperationalEscalationPolicy.is_active.is_(True))
        .filter(
            or_(
                OperationalEscalationPolicy.entity_type == OperationalEntityType.outage,
                OperationalEscalationPolicy.entity_type.is_(None),
            )
        )
        .order_by(OperationalEscalationPolicy.level.asc())
        .all()
    )
    return [policy for policy in policies if _policy_matches_incident(policy, incident)]


def _policy_matches_incident(
    policy: OperationalEscalationPolicy,
    incident: OutageIncident,
) -> bool:
    if not _scope_matches(policy, incident):
        return False
    if (
        policy.min_affected_customers is not None
        and (incident.affected_count or 0) < policy.min_affected_customers
    ):
        return False
    if policy.min_severity and not _severity_at_least(
        incident.severity,
        policy.min_severity,
    ):
        return False
    return True


def _scope_matches(
    policy: OperationalEscalationPolicy,
    incident: OutageIncident,
) -> bool:
    if not policy.scope_type or not policy.scope_id:
        return True
    scope_ids = {
        "network_device": incident.root_node_id,
        "node": incident.root_node_id,
        "root_node": incident.root_node_id,
        "pop_site": incident.basestation_id,
        "site": incident.basestation_id,
        "basestation": incident.basestation_id,
        "fdh_cabinet": incident.fdh_cabinet_id,
    }
    return str(scope_ids.get(policy.scope_type) or "") == policy.scope_id


def _severity_at_least(actual: str | None, minimum: str) -> bool:
    if actual is None:
        return False
    actual_key = actual.lower()
    minimum_key = minimum.lower()
    if actual_key not in SEVERITY_RANK or minimum_key not in SEVERITY_RANK:
        return actual_key == minimum_key
    return SEVERITY_RANK[actual_key] >= SEVERITY_RANK[minimum_key]


def _record_outage_event_once(
    session,
    incident: OutageIncident,
    *,
    policy: OperationalEscalationPolicy,
    trigger: str,
) -> OperationalEscalationEvent:
    existing = (
        session.query(OperationalEscalationEvent)
        .filter(OperationalEscalationEvent.entity_type == OperationalEntityType.outage)
        .filter(OperationalEscalationEvent.entity_id == str(incident.id))
        .filter(OperationalEscalationEvent.policy_id == policy.id)
        .filter(OperationalEscalationEvent.trigger == trigger)
        .filter(
            OperationalEscalationEvent.status.in_(
                [
                    OperationalEscalationStatus.open,
                    OperationalEscalationStatus.acknowledged,
                ]
            )
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    return operational_escalation.record_event(
        session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident.id,
        policy_id=policy.id,
        trigger=trigger,
        level=policy.level,
        severity=incident.severity,
        affected_customer_count=incident.affected_count,
        metadata={
            "status": incident.status,
            "scope": _scope_label(incident),
        },
    )
