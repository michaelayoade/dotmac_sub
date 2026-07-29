from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
    ServiceTeamExternalReference,
    ServiceTeamMember,
    ServiceTeamMemberResponsibility,
    ServiceTeamRelationship,
    ServiceTeamRelationshipType,
    ServiceTeamResponsibilityDefinition,
    ServiceTeamResponsibilityKey,
)
from app.models.system_user import SystemUser
from app.services import service_team_lifecycle
from app.services.owner_commands import CommandContext


@pytest.fixture(autouse=True)
def _registered_team_vocabulary(db_session):
    for key in ServiceTeamCapabilityKey:
        db_session.add(
            ServiceTeamCapabilityDefinition(
                key=key.value,
                name=key.value,
                description=f"Test definition for {key.value}",
                contract_owner="test",
            )
        )
    for key in ServiceTeamResponsibilityKey:
        db_session.add(
            ServiceTeamResponsibilityDefinition(
                key=key.value,
                name=key.value,
                description=f"Test definition for {key.value}",
                operational_scope="membership",
            )
        )
    db_session.commit()


def _context(operation: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{uuid4()}",
        scope=f"operations.service_team_lifecycle:{operation}",
        reason=f"test {operation}",
        idempotency_key=f"{operation}:{command_id}",
    )


def _staff(db_session, *, email: str, is_active: bool = True) -> tuple[UUID, UUID]:
    person = Party(
        party_type=PartyType.person.value,
        display_name="Ada Operator",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(person)
    db_session.flush()
    user = SystemUser(
        first_name="Ada",
        last_name="Operator",
        display_name="Ada Operator",
        email=email,
        is_active=is_active,
        person_party_id=person.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="service-team-test",
        party_binding_reason="Reviewed fixture identity",
    )
    db_session.add(user)
    db_session.flush()
    user_id = user.id
    person_id = person.id
    db_session.commit()
    return user_id, person_id


def _create_team(
    db_session,
    *,
    name: str = "Unified Operations",
    capabilities: tuple[ServiceTeamCapabilityKey, ...] = (
        ServiceTeamCapabilityKey.operations_general,
        ServiceTeamCapabilityKey.support_tickets,
    ),
) -> UUID:
    team_id = uuid4()
    result = service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=_context("create"),
            team_id=team_id,
            name=name,
            capability_keys=capabilities,
        ),
    )
    assert result.replayed is False
    return team_id


def test_team_has_many_capabilities_and_create_replays_exact_state(db_session):
    team_id = _create_team(db_session)

    replay = service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=_context("create-replay"),
            team_id=team_id,
            name="Unified Operations",
            capability_keys=(
                ServiceTeamCapabilityKey.support_tickets,
                ServiceTeamCapabilityKey.operations_general,
            ),
        ),
    )

    team = db_session.get(ServiceTeam, team_id)
    assert replay.replayed is True
    assert team is not None
    assert team.team_type == "composable"
    assert team.region is None
    assert team.manager_person_id is None
    assert {item.capability_key for item in team.capabilities if item.is_active} == {
        ServiceTeamCapabilityKey.operations_general.value,
        ServiceTeamCapabilityKey.support_tickets.value,
    }
    assert (
        db_session.query(AuditEvent).filter_by(action="service_team.created").count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="service_team.changed")
        .count()
        == 1
    )


def test_create_requires_registered_capability(db_session):
    definition = db_session.get(
        ServiceTeamCapabilityDefinition,
        ServiceTeamCapabilityKey.operations_general.value,
    )
    assert definition is not None
    definition.is_active = False
    db_session.commit()

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="not registered and active",
    ):
        _create_team(
            db_session,
            capabilities=(ServiceTeamCapabilityKey.operations_general,),
        )


def test_membership_has_many_responsibilities_and_no_manager_pointer(db_session):
    system_user_id, person_id = _staff(db_session, email="manager@example.com")
    team_id = _create_team(db_session)

    added = service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("add-member"),
            team_id=team_id,
            system_user_id=system_user_id,
            responsibility_keys=(
                ServiceTeamResponsibilityKey.accountable_manager,
                ServiceTeamResponsibilityKey.queue_lead,
                ServiceTeamResponsibilityKey.on_call,
            ),
        ),
    )

    member = db_session.get(ServiceTeamMember, added.member_id)
    team = db_session.get(ServiceTeam, team_id)
    assert member is not None
    assert member.person_id == person_id
    assert member.role == "member"
    assert team is not None and team.manager_person_id is None
    assert {
        row.responsibility_key for row in member.responsibilities if row.is_active
    } == {
        ServiceTeamResponsibilityKey.accountable_manager.value,
        ServiceTeamResponsibilityKey.queue_lead.value,
        ServiceTeamResponsibilityKey.on_call.value,
    }


def test_legacy_manager_pointer_remains_drift_until_explicit_composition(db_session):
    system_user_id, person_id = _staff(
        db_session,
        email="legacy-manager@example.com",
    )
    team_id = _create_team(db_session, name="Legacy Manager Shadow")
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    team.manager_person_id = person_id
    db_session.commit()

    before = next(
        item
        for item in service_team_lifecycle.list_teams(db_session).items
        if item.team_id == team_id
    )
    assert before.accountable_manager_labels == ()
    assert before.legacy_shadow_drift is True
    assert before.legacy_shadow_issues == (
        service_team_lifecycle.ServiceTeamLegacyShadowIssue.manager_requires_explicit_composition,
    )
    assert db_session.query(ServiceTeamMember).filter_by(team_id=team_id).count() == 0
    db_session.commit()

    service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("explicit-manager-composition"),
            team_id=team_id,
            system_user_id=system_user_id,
            responsibility_keys=(ServiceTeamResponsibilityKey.accountable_manager,),
        ),
    )

    after = next(
        item
        for item in service_team_lifecycle.list_teams(db_session).items
        if item.team_id == team_id
    )
    assert after.accountable_manager_labels == ("Ada Operator",)
    assert after.legacy_shadow_drift is False
    assert after.legacy_shadow_issues == ()
    assert db_session.get(ServiceTeam, team_id).manager_person_id == person_id


def test_legacy_shadow_audit_classifies_scalar_and_member_role_drift(db_session):
    _system_user_id, person_id = _staff(
        db_session,
        email="legacy-lead@example.com",
    )
    team_id = _create_team(db_session, name="Legacy Shadow Audit")
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    team.team_type = "support"
    team.region = "Abuja"
    member = ServiceTeamMember(
        team_id=team_id,
        person_id=person_id,
        role="lead",
        is_active=True,
    )
    db_session.add(member)
    db_session.commit()

    audit = service_team_lifecycle.audit_legacy_service_team_shadow(db_session)

    assert audit.ready is False
    assert audit.team_count == 1
    assert audit.drift_team_count == 1
    assert dict(audit.issue_counts) == {
        (
            service_team_lifecycle.ServiceTeamLegacyShadowIssue.team_type_capability_mismatch
        ): 1,
        (
            service_team_lifecycle.ServiceTeamLegacyShadowIssue.region_requires_geo_area_review
        ): 1,
        (
            service_team_lifecycle.ServiceTeamLegacyShadowIssue.manager_requires_explicit_composition
        ): 0,
        (
            service_team_lifecycle.ServiceTeamLegacyShadowIssue.member_role_responsibility_mismatch
        ): 1,
    }

    db_session.add(
        ServiceTeamMemberResponsibility(
            membership_id=member.id,
            responsibility_key=ServiceTeamResponsibilityKey.queue_lead.value,
            is_active=True,
        )
    )
    db_session.commit()

    after = service_team_lifecycle.audit_legacy_service_team_shadow(db_session)
    assert (
        dict(after.issue_counts)[
            (
                service_team_lifecycle.ServiceTeamLegacyShadowIssue.member_role_responsibility_mismatch
            )
        ]
        == 0
    )


def test_responsibilities_can_be_replaced_compositionally(db_session):
    system_user_id, _ = _staff(db_session, email="agent@example.com")
    team_id = _create_team(db_session)
    added = service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("add"),
            team_id=team_id,
            system_user_id=system_user_id,
            responsibility_keys=(ServiceTeamResponsibilityKey.agent,),
        ),
    )

    service_team_lifecycle.set_member_responsibilities(
        db_session,
        service_team_lifecycle.SetServiceTeamMemberResponsibilities(
            context=_context("responsibilities"),
            team_id=team_id,
            member_id=added.member_id,
            responsibility_keys=(
                ServiceTeamResponsibilityKey.dispatcher,
                ServiceTeamResponsibilityKey.on_call,
            ),
        ),
    )

    active = set(
        db_session.scalars(
            ServiceTeamMemberResponsibility.__table__.select()
            .with_only_columns(ServiceTeamMemberResponsibility.responsibility_key)
            .where(
                ServiceTeamMemberResponsibility.membership_id == added.member_id,
                ServiceTeamMemberResponsibility.is_active.is_(True),
            )
        ).all()
    )
    assert active == {"dispatcher", "on_call"}


def test_multi_team_resolution_returns_a_set_not_an_ambiguity(db_session):
    system_user_id, _ = _staff(db_session, email="multi@example.com")
    first = _create_team(db_session, name="Support and NOC")
    second = _create_team(
        db_session,
        name="Field and NOC",
        capabilities=(ServiceTeamCapabilityKey.field_service_work_orders,),
    )
    for team_id in (first, second):
        service_team_lifecycle.add_member(
            db_session,
            service_team_lifecycle.AddServiceTeamMember(
                context=_context(f"member-{team_id}"),
                team_id=team_id,
                system_user_id=system_user_id,
                responsibility_keys=(ServiceTeamResponsibilityKey.agent,),
            ),
        )

    resolved = service_team_lifecycle.resolve_staff_service_teams(
        db_session, system_user_id
    )

    assert resolved.kind is service_team_lifecycle.ServiceTeamResolutionKind.resolved
    assert set(resolved.team_ids) == {first, second}


def test_queue_scope_uses_responsibility_without_granting_rbac(db_session):
    system_user_id, _ = _staff(db_session, email="lead@example.com")
    first = _create_team(db_session, name="Lead Team")
    second = _create_team(db_session, name="Agent Team")
    for team_id, responsibility in (
        (first, ServiceTeamResponsibilityKey.queue_lead),
        (second, ServiceTeamResponsibilityKey.agent),
    ):
        service_team_lifecycle.add_member(
            db_session,
            service_team_lifecycle.AddServiceTeamMember(
                context=_context(f"scope-{team_id}"),
                team_id=team_id,
                system_user_id=system_user_id,
                responsibility_keys=(responsibility,),
            ),
        )

    scope = service_team_lifecycle.resolve_staff_team_scope(db_session, system_user_id)

    assert set(scope.member_team_ids) == {first, second}
    assert scope.queue_lead_team_ids == (first,)
    assert scope.accountable_manager_team_ids == ()
    assert scope.queue_scope_team_ids == (first,)


def test_team_relationship_rejects_cycles(db_session):
    parent = _create_team(db_session, name="Parent")
    child = _create_team(db_session, name="Child")
    service_team_lifecycle.set_team_relationship(
        db_session,
        service_team_lifecycle.SetServiceTeamRelationship(
            context=_context("parent-child"),
            relationship_id=uuid4(),
            parent_team_id=parent,
            child_team_id=child,
            relationship_type=ServiceTeamRelationshipType.organizational_parent,
            is_active=True,
        ),
    )

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="cycle",
    ):
        service_team_lifecycle.set_team_relationship(
            db_session,
            service_team_lifecycle.SetServiceTeamRelationship(
                context=_context("child-parent"),
                relationship_id=uuid4(),
                parent_team_id=child,
                child_team_id=parent,
                relationship_type=ServiceTeamRelationshipType.organizational_parent,
                is_active=True,
            ),
        )
    assert db_session.query(ServiceTeamRelationship).count() == 1


def test_multiple_external_references_are_observations(db_session):
    team_id = _create_team(db_session)
    for system, reference in (("crm", "department-7"), ("workforce", "unit-22")):
        service_team_lifecycle.observe_external_reference(
            db_session,
            service_team_lifecycle.ObserveServiceTeamExternalReference(
                context=_context(system),
                reference_id=uuid4(),
                team_id=team_id,
                system=system,
                entity_type="department",
                external_reference=reference,
                observed_at=datetime.now(UTC),
                is_active=True,
            ),
        )

    references = db_session.query(ServiceTeamExternalReference).all()
    assert {(item.system, item.external_reference) for item in references} == {
        ("crm", "department-7"),
        ("workforce", "unit-22"),
    }


def test_update_fails_closed_on_stale_state(db_session):
    team_id = _create_team(db_session)
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    stale_updated_at = team.updated_at - timedelta(days=1)
    db_session.commit()

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as error:
        service_team_lifecycle.update_team(
            db_session,
            service_team_lifecycle.UpdateServiceTeam(
                context=_context("stale"),
                team_id=team_id,
                expected_updated_at=stale_updated_at,
                name="Changed",
                capability_keys=(ServiceTeamCapabilityKey.support_tickets,),
            ),
        )

    assert error.value.code == "service_team_stale"
