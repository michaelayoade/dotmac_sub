from __future__ import annotations

from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
from app.models.operational_escalation import (
    OperationalEntityType,
    OperationalOwner,
    OperationalOwnerRole,
    OperationalRoomProvider,
    OperationalWatcherRole,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.services import operational_escalation


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
