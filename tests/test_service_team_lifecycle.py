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
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
    ServiceTeamDepartmentMembershipSource,
    ServiceTeamExternalReference,
    ServiceTeamMember,
    ServiceTeamMemberResponsibility,
    ServiceTeamResponsibilityKey,
)
from app.models.system_user import SystemUser
from app.services import service_team_composition, service_team_lifecycle
from app.services.owner_commands import CommandContext


@pytest.fixture(autouse=True)
def _plain_reads_after_commit(db_session):
    # Owner commands require a transaction-free session at entry. Keep fixture
    # attributes loaded across commits (as app adapters do via fresh_reads) so
    # reading `team.id` between commands does not autobegin a transaction.
    db_session.expire_on_commit = False


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


def _staff(
    db_session,
    *,
    email: str,
    is_active: bool = True,
    party_status: PartyIdentityStatus = PartyIdentityStatus.active,
) -> tuple[UUID, UUID]:
    person = Party(
        party_type=PartyType.person.value,
        display_name="Ada Operator",
        status=party_status.value,
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
    db_session.commit()
    return user.id, person.id


def _create_team(db_session, name: str) -> UUID:
    team_id = uuid4()
    outcome = service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=_context("create"),
            team_id=team_id,
            name=name,
        ),
    )
    assert outcome.team_id == team_id
    return team_id


def _map_erp_department(
    db_session,
    *,
    team_id: UUID,
    department_id: str,
    account_scope: str = "erp-org-1",
) -> None:
    db_session.add(
        ServiceTeamExternalReference(
            team_id=team_id,
            provider=service_team_lifecycle.ERP_DEPARTMENT_PROVIDER,
            account_scope=account_scope,
            external_id=department_id,
            provenance="reviewed ERP department mapping",
            observed_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db_session.commit()


def _sync_erp_department(
    db_session,
    *,
    user_id: UUID,
    employee_id: str,
    department_id: str | None,
    account_scope: str = "erp-org-1",
) -> service_team_lifecycle.ErpDepartmentMembershipMutation:
    department = (
        service_team_lifecycle.ErpDepartmentMembershipDepartment(
            department_id=department_id,
            department_code="SUPPORT",
            department_name="Support",
        )
        if department_id is not None
        else None
    )
    return service_team_lifecycle.sync_erp_department_membership(
        db_session,
        service_team_lifecycle.SyncErpDepartmentMembership(
            context=_context("erp-department-sync"),
            system_user_id=user_id,
            account_scope=account_scope,
            erp_employee_id=employee_id,
            employee_code="EMP-001",
            department=department,
            observed_at=datetime.now(UTC),
        ),
    )


def test_team_identity_create_replays_without_scalar_authority(db_session):
    team_id = _create_team(db_session, "Unified Operations")
    team = db_session.get(ServiceTeam, team_id)

    assert team is not None
    assert team.team_type is None
    assert team.region is None
    assert team.manager_person_id is None
    assert team.workforce_system is None
    assert team.workforce_department_reference is None

    # Release the read transaction before entering the next public owner command.
    db_session.commit()
    replay = service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=_context("create-replay"),
            team_id=team_id,
            name="Unified Operations",
        ),
    )
    assert replay.replayed is True
    assert db_session.query(ServiceTeam).count() == 1
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "service_team.created")
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "service_team.changed")
        .count()
        == 1
    )


def test_team_identity_rejects_uuid_collision_duplicate_name_and_stale_update(
    db_session,
):
    team_id = _create_team(db_session, "Field Operations")
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected = team.updated_at
    db_session.commit()

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as collision:
        service_team_lifecycle.create_team(
            db_session,
            service_team_lifecycle.CreateServiceTeam(
                context=_context("collision"),
                team_id=team_id,
                name="Different Identity",
            ),
        )
    assert collision.value.code == "service_team_identity_collision"

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as duplicate:
        service_team_lifecycle.create_team(
            db_session,
            service_team_lifecycle.CreateServiceTeam(
                context=_context("duplicate"),
                team_id=uuid4(),
                name="field operations",
            ),
        )
    assert duplicate.value.code == "service_team_name_conflict"

    replay = service_team_lifecycle.update_team(
        db_session,
        service_team_lifecycle.UpdateServiceTeam(
            context=_context("update-replay"),
            team_id=team_id,
            expected_updated_at=expected - timedelta(days=1),
            name="Field Operations",
        ),
    )
    assert replay.replayed is True

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as stale:
        service_team_lifecycle.update_team(
            db_session,
            service_team_lifecycle.UpdateServiceTeam(
                context=_context("stale-update"),
                team_id=team_id,
                expected_updated_at=expected - timedelta(days=1),
                name="Field Delivery",
            ),
        )
    assert stale.value.code == "service_team_stale"


def test_membership_has_no_authoritative_scalar_role(db_session):
    user_id, person_id = _staff(db_session, email="member@example.com")
    team_id = _create_team(db_session, "Shared Delivery")

    outcome = service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("add-member"),
            team_id=team_id,
            system_user_id=user_id,
        ),
    )
    member = db_session.get(ServiceTeamMember, outcome.member_id)

    assert member is not None
    assert member.person_id == person_id
    # The column remains only as a migration shadow and native owner writes no role.
    assert member.role is None


def test_membership_removal_allows_soft_team_retirement(db_session):
    user_id, _person_id = _staff(db_session, email="retire@example.com")
    team_id = _create_team(db_session, "Retiring Team")
    added = service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("add-before-retire"),
            team_id=team_id,
            system_user_id=user_id,
        ),
    )
    assert added.member_id is not None

    removed = service_team_lifecycle.remove_member(
        db_session,
        service_team_lifecycle.RemoveServiceTeamMember(
            context=_context("remove-before-retire"),
            team_id=team_id,
            member_id=added.member_id,
            reason="Staff moved to another team.",
        ),
    )
    assert removed.replayed is False
    member = db_session.get(ServiceTeamMember, added.member_id)
    assert member is not None
    assert member.is_active is False
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected_updated_at = team.updated_at
    db_session.commit()

    retired = service_team_lifecycle.set_team_active(
        db_session,
        service_team_lifecycle.SetServiceTeamActive(
            context=_context("retire-team"),
            team_id=team_id,
            expected_updated_at=expected_updated_at,
            is_active=False,
            reason="Operational group replaced.",
        ),
    )

    assert retired.replayed is False
    assert db_session.get(ServiceTeam, team_id).is_active is False


def test_inactive_or_archived_staff_cannot_be_assigned(db_session):
    inactive_id, _party_id = _staff(
        db_session,
        email="inactive@example.com",
        is_active=False,
    )
    archived_id, _archived_party_id = _staff(
        db_session,
        email="archived@example.com",
        party_status=PartyIdentityStatus.archived,
    )
    team_id = _create_team(db_session, "Reviewed Identity Team")

    for user_id in (inactive_id, archived_id):
        with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as error:
            service_team_lifecycle.add_member(
                db_session,
                service_team_lifecycle.AddServiceTeamMember(
                    context=_context("invalid-member"),
                    team_id=team_id,
                    system_user_id=user_id,
                ),
            )
        assert error.value.code in {
            "service_team_staff_not_found",
            "service_team_staff_identity_invalid",
        }


def test_admin_projection_composes_capabilities_and_many_responsibilities(db_session):
    user_id, person_id = _staff(db_session, email="projection@example.com")
    team_id = _create_team(db_session, "Composed Projection")
    added = service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("projection-member"),
            team_id=team_id,
            system_user_id=user_id,
        ),
    )
    assert added.member_id is not None
    capability = ServiceTeamCapabilityKey.customer_support
    contract = service_team_composition.CAPABILITY_CONTRACTS[capability]
    db_session.add_all(
        [
            ServiceTeamCapabilityDefinition(
                key=capability.value,
                display_name=contract.display_name,
                contract_owner=contract.contract_owner,
                contract_version=contract.contract_version,
                description="Test projection definition",
                is_active=True,
            ),
            ServiceTeamCapability(
                team_id=team_id,
                capability_key=capability.value,
                is_active=True,
            ),
            ServiceTeamMemberResponsibility(
                membership_id=added.member_id,
                responsibility_key=(
                    ServiceTeamResponsibilityKey.accountable_manager.value
                ),
                is_active=True,
            ),
            ServiceTeamMemberResponsibility(
                membership_id=added.member_id,
                responsibility_key=ServiceTeamResponsibilityKey.dispatcher.value,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    result = service_team_lifecycle.list_teams(
        db_session,
        search="composed",
        is_active=True,
    )
    detail = service_team_lifecycle.get_team(db_session, team_id)

    assert result.total == 1
    assert result.items[0].capabilities == (capability,)
    assert result.items[0].accountable_manager_labels == ("Ada Operator",)
    assert detail.members[0].person_id == person_id
    assert set(detail.members[0].responsibilities) == {
        ServiceTeamResponsibilityKey.accountable_manager,
        ServiceTeamResponsibilityKey.dispatcher,
    }


def test_staff_team_resolution_returns_every_membership_as_a_set(db_session):
    user_id, person_id = _staff(db_session, email="multi@example.com")
    first_id = _create_team(db_session, "Support and Field")
    second_id = _create_team(db_session, "Outage Response")
    db_session.add_all(
        [
            ServiceTeamMember(
                team_id=first_id,
                person_id=person_id,
                role=None,
                is_active=True,
            ),
            ServiceTeamMember(
                team_id=second_id,
                person_id=person_id,
                role=None,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    resolution = service_team_lifecycle.resolve_staff_service_teams(
        db_session,
        user_id,
    )

    assert resolution.kind is service_team_lifecycle.ServiceTeamResolutionKind.resolved
    assert set(resolution.team_ids) == {first_id, second_id}
    assert resolution.team_id is None
    assert resolution.candidate_team_ids == resolution.team_ids


def test_erp_department_sync_adds_tracked_service_team_membership(db_session):
    user_id, person_id = _staff(db_session, email="erp-assigned@example.com")
    team_id = _create_team(db_session, "ERP Support")
    _map_erp_department(db_session, team_id=team_id, department_id="dept-support")

    outcome = _sync_erp_department(
        db_session,
        user_id=user_id,
        employee_id="employee-1",
        department_id="dept-support",
    )

    assert outcome.team_id == team_id
    assert outcome.previous_team_id is None
    assert outcome.replayed is False
    member = db_session.get(ServiceTeamMember, outcome.member_id)
    assert member is not None
    assert member.person_id == person_id
    assert member.is_active is True
    source = db_session.query(ServiceTeamDepartmentMembershipSource).one()
    assert source.provider == service_team_lifecycle.ERP_DEPARTMENT_PROVIDER
    assert source.external_employee_id == "employee-1"
    assert source.external_department_id == "dept-support"
    assert source.member_id == member.id
    assert source.is_active is True


def test_erp_department_sync_transfers_only_erp_managed_membership(db_session):
    user_id, person_id = _staff(db_session, email="erp-transfer@example.com")
    first_team_id = _create_team(db_session, "ERP Support East")
    second_team_id = _create_team(db_session, "ERP Support West")
    manual_team_id = _create_team(db_session, "Manual Escalation")
    _map_erp_department(db_session, team_id=first_team_id, department_id="dept-east")
    _map_erp_department(db_session, team_id=second_team_id, department_id="dept-west")
    db_session.add(
        ServiceTeamMember(
            team_id=manual_team_id,
            person_id=person_id,
            role=None,
            is_active=True,
        )
    )
    db_session.commit()

    first = _sync_erp_department(
        db_session,
        user_id=user_id,
        employee_id="employee-transfer",
        department_id="dept-east",
    )
    assert first.member_id is not None
    db_session.commit()
    transferred = _sync_erp_department(
        db_session,
        user_id=user_id,
        employee_id="employee-transfer",
        department_id="dept-west",
    )

    assert transferred.team_id == second_team_id
    assert transferred.previous_team_id == first_team_id
    assert transferred.replayed is False
    assert db_session.get(ServiceTeamMember, first.member_id).is_active is False
    active_team_ids = {
        member.team_id
        for member in db_session.query(ServiceTeamMember)
        .filter(ServiceTeamMember.person_id == person_id)
        .filter(ServiceTeamMember.is_active.is_(True))
    }
    assert active_team_ids == {second_team_id, manual_team_id}


def test_erp_department_removal_deactivates_tracked_membership_for_inactive_user(
    db_session,
):
    user_id, _person_id = _staff(db_session, email="erp-removal@example.com")
    team_id = _create_team(db_session, "ERP Removals")
    _map_erp_department(db_session, team_id=team_id, department_id="dept-remove")
    assigned = _sync_erp_department(
        db_session,
        user_id=user_id,
        employee_id="employee-remove",
        department_id="dept-remove",
    )
    user = db_session.get(SystemUser, user_id)
    assert user is not None
    user.is_active = False
    db_session.commit()

    removed = _sync_erp_department(
        db_session,
        user_id=user_id,
        employee_id="employee-remove",
        department_id=None,
    )

    assert removed.team_id is None
    assert removed.previous_team_id == team_id
    assert removed.replayed is False
    assert db_session.get(ServiceTeamMember, assigned.member_id).is_active is False
    assert (
        db_session.query(ServiceTeamDepartmentMembershipSource).one().is_active is False
    )


def test_erp_department_sync_rejects_unmapped_department(db_session):
    user_id, _person_id = _staff(db_session, email="erp-unmapped@example.com")

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as error:
        _sync_erp_department(
            db_session,
            user_id=user_id,
            employee_id="employee-unmapped",
            department_id="missing-department",
        )

    assert error.value.code == "service_team_erp_department_unmapped"
    assert db_session.query(ServiceTeamMember).count() == 0


def test_erp_department_sync_rejects_employee_identity_reuse(db_session):
    first_user_id, _first_person_id = _staff(
        db_session,
        email="erp-first@example.com",
    )
    second_user_id, _second_person_id = _staff(
        db_session,
        email="erp-second@example.com",
    )
    team_id = _create_team(db_session, "ERP Identity Conflict")
    _map_erp_department(db_session, team_id=team_id, department_id="dept-conflict")
    _sync_erp_department(
        db_session,
        user_id=first_user_id,
        employee_id="employee-conflict",
        department_id="dept-conflict",
    )
    db_session.commit()

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as error:
        _sync_erp_department(
            db_session,
            user_id=second_user_id,
            employee_id="employee-conflict",
            department_id=None,
        )

    assert error.value.code == "service_team_erp_employee_identity_conflict"


def test_team_deactivation_is_blocked_by_active_membership(db_session):
    user_id, _person_id = _staff(db_session, email="active@example.com")
    team_id = _create_team(db_session, "Active Team")
    service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("add-member"),
            team_id=team_id,
            system_user_id=user_id,
        ),
    )
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected_updated_at = team.updated_at
    db_session.commit()

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as error:
        service_team_lifecycle.set_team_active(
            db_session,
            service_team_lifecycle.SetServiceTeamActive(
                context=_context("deactivate"),
                team_id=team_id,
                expected_updated_at=expected_updated_at,
                is_active=False,
                reason="Replace team",
            ),
        )

    assert error.value.code == "service_team_has_active_members"
