from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.services.owner_commands import CommandContext
from app.services.service_team_pointer_retirement import (
    RetireLegacyServiceTeamPointers,
    ServiceTeamPointerRetirementApproval,
    ServiceTeamPointerRetirementError,
    audit_service_team_pointer_retirement,
    build_pointer_retirement_plan,
    legacy_manager_pointers,
    pointer_snapshot_sha256,
    retire_legacy_service_team_pointers,
)


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext.system(
        actor="service:pointer-retirement-test",
        scope="operations.service_team_pointer_retirement:retire",
        reason="Reviewed legacy pointer retirement test.",
        command_id=command_id,
        correlation_id=command_id,
        idempotency_key=f"pointer-retirement:{command_id}",
    )


def _legacy_pointer(db_session) -> tuple[ServiceTeam, Party]:
    archived = Party(
        party_type=PartyType.person.value,
        display_name=f"Archived {uuid4()}",
        status=PartyIdentityStatus.archived.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(archived)
    db_session.flush()
    team = ServiceTeam(
        name=f"Legacy Team {uuid4()}",
        team_type="support",
        manager_person_id=archived.id,
    )
    db_session.add(team)
    db_session.commit()
    return team, archived


def _command(db_session) -> RetireLegacyServiceTeamPointers:
    now = datetime.now(UTC)
    plan = build_pointer_retirement_plan(db_session, planned_at=now)
    plan_file_sha256 = "d" * 64
    command = RetireLegacyServiceTeamPointers(
        context=_context(),
        plan=plan,
        approval=ServiceTeamPointerRetirementApproval(
            plan_digest=plan.plan_digest,
            plan_file_sha256=plan_file_sha256,
            approved_by="identity-reviewer",
            approved_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            reason="Retire only the reviewed stale manager pointers.",
            maximum_pointers=len(plan.pointers),
        ),
        plan_file_sha256=plan_file_sha256,
    )
    db_session.commit()
    return command


def test_gate_reports_only_narrow_pointer_blockers(db_session):
    _team, archived = _legacy_pointer(db_session)

    audit = audit_service_team_pointer_retirement(db_session)

    assert audit.ready is False
    assert audit.legacy_manager_pointer_count == 1
    assert audit.membership_count == 0
    assert str(archived.id) not in str(audit.summary())


def test_exact_approved_five_pointer_plan_clears_only_manager_fields(db_session):
    teams = [_legacy_pointer(db_session)[0] for _ in range(5)]
    party_count = db_session.query(Party).count()
    member_count = db_session.query(ServiceTeamMember).count()
    command = _command(db_session)
    current = legacy_manager_pointers(db_session)
    assert len(current) == 5
    assert pointer_snapshot_sha256(current) == command.plan.source_snapshot_sha256
    db_session.commit()

    outcome = retire_legacy_service_team_pointers(db_session, command)

    assert outcome.retired_pointer_count == 5
    assert all(
        db_session.get(ServiceTeam, team.id).manager_person_id is None for team in teams
    )
    assert db_session.query(Party).count() == party_count
    assert db_session.query(ServiceTeamMember).count() == member_count
    assert audit_service_team_pointer_retirement(db_session).ready is True
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="service_team.legacy_manager_pointer_retired")
        .count()
        == 5
    )


def test_stale_pointer_snapshot_fails_closed_without_partial_changes(db_session):
    first, _ = _legacy_pointer(db_session)
    command = _command(db_session)
    second, _ = _legacy_pointer(db_session)

    with pytest.raises(ServiceTeamPointerRetirementError) as error:
        retire_legacy_service_team_pointers(db_session, command)

    assert error.value.code.endswith(".stale_source")
    assert db_session.get(ServiceTeam, first.id).manager_person_id is not None
    assert db_session.get(ServiceTeam, second.id).manager_person_id is not None
