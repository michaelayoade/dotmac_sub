from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.operational_escalation import (
    OutageTeamRoutingPolicy,
    OutageTeamRoutingPurpose,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
)
from app.services.owner_commands import CommandContext
from app.services.topology import outage_team_routing


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{uuid4()}",
        scope="network.outage_team_routing:set_outage_team_route",
        reason="Test exact outage routing.",
        idempotency_key=f"outage-route:{command_id}",
    )


def _team(db_session, capability: ServiceTeamCapabilityKey):
    if db_session.get(ServiceTeamCapabilityDefinition, capability.value) is None:
        db_session.add(
            ServiceTeamCapabilityDefinition(
                key=capability.value,
                name=capability.value,
                description="Test capability",
                contract_owner="network.outage_team_routing",
            )
        )
        db_session.flush()
    team = ServiceTeam(name=f"Team {uuid4()}", team_type="composable")
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamCapability(
            team_id=team.id,
            capability_key=capability.value,
        )
    )
    team_id = team.id
    db_session.commit()
    return team_id


def test_route_requires_the_governed_capability(db_session):
    team_id = _team(db_session, ServiceTeamCapabilityKey.support_tickets)

    with pytest.raises(
        outage_team_routing.OutageTeamRoutingError,
        match="lacks the required outage capability",
    ):
        outage_team_routing.set_outage_team_route(
            db_session,
            outage_team_routing.SetOutageTeamRoute(
                context=_context(),
                policy_id=uuid4(),
                purpose=OutageTeamRoutingPurpose.primary_owner,
                service_team_id=team_id,
                priority=10,
                is_active=True,
            ),
        )


def test_primary_route_is_exact_replayable_and_unique(db_session):
    first_id = _team(db_session, ServiceTeamCapabilityKey.network_outages_coordinate)
    second_id = _team(db_session, ServiceTeamCapabilityKey.network_outages_coordinate)
    policy_id = uuid4()
    command = outage_team_routing.SetOutageTeamRoute(
        context=_context(),
        policy_id=policy_id,
        purpose=OutageTeamRoutingPurpose.primary_owner,
        service_team_id=first_id,
        priority=10,
        is_active=True,
    )

    created = outage_team_routing.set_outage_team_route(db_session, command)
    replay = outage_team_routing.set_outage_team_route(db_session, command)

    assert created.replayed is False
    assert replay.replayed is True
    policy = db_session.get(OutageTeamRoutingPolicy, policy_id)
    assert policy is not None and policy.service_team_id == first_id
    db_session.commit()

    with pytest.raises(
        outage_team_routing.OutageTeamRoutingError,
        match="Deactivate the current primary",
    ):
        outage_team_routing.set_outage_team_route(
            db_session,
            outage_team_routing.SetOutageTeamRoute(
                context=_context(),
                policy_id=uuid4(),
                purpose=OutageTeamRoutingPurpose.primary_owner,
                service_team_id=second_id,
                priority=20,
                is_active=True,
            ),
        )
