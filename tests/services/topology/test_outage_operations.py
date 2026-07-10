from __future__ import annotations

from datetime import UTC, datetime

from app.models.network_monitoring import NetworkDevice
from app.models.operational_escalation import (
    OperationalEntityType,
    OperationalOwner,
    OperationalParticipantType,
    OperationalRoomLink,
    OperationalWatcher,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.services import operational_escalation
from app.services.topology.outage import (
    confirm_incident,
    declare_outage,
    open_classifier_incident,
)


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _seed_ops_teams(db_session):
    return {
        "operations": _team(
            db_session,
            "NOC",
            ServiceTeamType.operations.value,
        ),
        "support": _team(
            db_session,
            "Support",
            ServiceTeamType.support.value,
        ),
        "field": _team(
            db_session,
            "Field Service",
            ServiceTeamType.field_service.value,
        ),
    }


def _node(db_session) -> NetworkDevice:
    node = NetworkDevice(name="Garki OLT", is_active=True)
    db_session.add(node)
    db_session.flush()
    return node


def test_declare_outage_creates_default_owner_watchers_and_room(db_session):
    teams = _seed_ops_teams(db_session)
    node = _node(db_session)

    incident = declare_outage(
        db_session,
        node=node,
        declared_by="noc@dotmac.io",
        severity="high",
        impact={"count": 184},
    )

    owner = db_session.query(OperationalOwner).one()
    watchers = db_session.query(OperationalWatcher).all()
    room = db_session.query(OperationalRoomLink).one()
    assert owner.entity_type == OperationalEntityType.outage
    assert owner.entity_id == str(incident.id)
    assert owner.service_team_id == teams["operations"].id
    assert owner.metadata_["affected_count"] == 184
    assert {watcher.service_team_id for watcher in watchers} == {
        teams["operations"].id,
        teams["support"].id,
        teams["field"].id,
    }
    assert {watcher.watcher_type for watcher in watchers} == {
        OperationalParticipantType.team
    }
    assert room.provider == "nextcloud_talk"
    assert room.metadata_["provisioning_status"] == "planned"
    assert "GARKI-OLT" in room.room_id


def test_classifier_outage_creates_operations_state_only_when_confirmed(db_session):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)

    incident = open_classifier_incident(
        db_session,
        root_node=node,
        affected_count=20,
        now=now,
    )

    assert db_session.query(OperationalOwner).count() == 0
    assert db_session.query(OperationalWatcher).count() == 0

    confirm_incident(db_session, incident, now=now)

    assert db_session.query(OperationalOwner).count() == 1
    assert db_session.query(OperationalWatcher).count() == 3
    assert db_session.query(OperationalRoomLink).count() == 1


def test_outage_operations_preserves_existing_primary_owner(db_session):
    teams = _seed_ops_teams(db_session)
    node = _node(db_session)
    incident = declare_outage(
        db_session,
        node=node,
        impact={"count": 4},
    )
    custom_owner = operational_escalation.set_owner(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident.id,
        service_team_id=teams["field"].id,
        source="manual",
    )

    from app.services.topology.outage_operations import ensure_outage_operations

    ensure_outage_operations(db_session, incident)

    active_owners = (
        db_session.query(OperationalOwner)
        .filter(OperationalOwner.entity_type == OperationalEntityType.outage)
        .filter(OperationalOwner.entity_id == str(incident.id))
        .filter(OperationalOwner.is_active.is_(True))
        .all()
    )
    assert active_owners == [custom_owner]
    assert active_owners[0].service_team_id == teams["field"].id
