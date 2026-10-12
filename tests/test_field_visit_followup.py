"""Fast owner checks; real migrated PostgreSQL tests are separately collected."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.dispatch import TechnicianProfile
from app.models.field_job_event import FieldJobEvent
from app.models.field_visit_followup import (
    FieldVisitFollowup,
    FieldVisitFollowupReceipt,
    FieldVisitFollowupRevision,
)
from app.models.service_team import ServiceTeam
from app.models.work_order import WorkOrder
from app.services import owner_commands, sot_relationships
from app.services.domain_errors import DomainError
from app.services.field import visit_followup as owner
from app.services.field.visit_followup_contracts import (
    OWNER,
    READ,
    WRITE,
    DecideVisitFollowup,
    FailedVisitIntakeQuery,
    FailureSource,
    FailureSourceKind,
    FollowupDisposition,
    FollowupResponsibility,
    OpenVisitFollowup,
    VisitFollowupQueueQuery,
    VisitFollowupWorkspaceQuery,
)
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.sot_registry.domains.provisioning_operations.visit_followup import (
    SERVICES,
)
from app.services.staff_permission_admission import AuthorizedStaffPerson
from tests.staff_identity_fixtures import add_bound_staff_user


@pytest.fixture
def followup_rig(db_session, monkeypatch):
    db = db_session
    staff, person = add_bound_staff_user(db)
    technician, _ = add_bound_staff_user(db)
    profile = TechnicianProfile(system_user_id=technician.id, person_id=technician.id)
    job = WorkOrder(
        title="No access visit",
        status="canceled",
        work_order_kind="infrastructure",
        public_id="sub-followup-routing",
    )
    team = ServiceTeam(name="Field triage", is_active=True)
    db.add_all((profile, job, team))
    db.flush()
    event = FieldJobEvent(
        work_order_mirror_id=job.id,
        author_technician_id=profile.id,
        person_id=technician.id,
        system_user_id=technician.id,
        event="unable_to_complete",
        new_status="canceled",
        assignment_generation=uuid4(),
        occurred_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        client_event_id=uuid4(),
        payload={"reason": "no_access"},
    )
    db.add(event)
    db.flush()
    access = PermissionAdmission(
        staff.id,
        "system_user",
        frozenset((READ, WRITE, owner.COMPLETION_READ, owner.COMPLETION_WRITE)),
    )
    command = OpenVisitFollowup(
        CommandContext.system(
            actor=str(staff.id),
            scope=WRITE,
            reason="Dispatch owns the access followup",
            idempotency_key=str(uuid4()),
        ),
        access,
        FailureSource(FailureSourceKind.unable_event, event.id),
        FollowupResponsibility(team.id, None, datetime.now(UTC) + timedelta(days=1)),
        "Dispatch owns the access followup",
    )
    ids = (staff.id, person.id, job.id, team.id)
    db.commit()

    def gate(db, *, admission, permission, write):
        if (
            permission not in admission.admitted_permissions
            or admission.principal_id != ids[0]
        ):
            raise owner._error("denied", "Current staff permission required")
        return AuthorizedStaffPerson(ids[0], ids[1])

    monkeypatch.setattr(owner, "revalidate_staff_admission", gate)
    original = sot_relationships.service_relationship
    monkeypatch.setattr(
        sot_relationships,
        "service_relationship",
        lambda name: SERVICES[0] if name == OWNER else original(name),
    )
    all_services = sot_relationships.all_services
    monkeypatch.setattr(
        sot_relationships, "all_services", lambda: (*all_services(), SERVICES[0])
    )
    owner_commands._validate_manifest.cache_clear()
    monkeypatch.setattr(owner, "_readiness", lambda *args, **kwargs: None)
    return db, command, ids


def decision(command, outcome, disposition=FollowupDisposition.revisit_required):
    return DecideVisitFollowup(
        replace(command.context, idempotency_key=str(uuid4())),
        command.access,
        outcome.obligation_id,
        outcome.revision_id,
        command.responsibility,
        disposition,
        command.reason,
    )


def test_exact_source_pending_until_accountable_and_queue_visible(followup_rig):
    db, command, ids = followup_rig
    pending = owner.pending_failures(
        db, query=FailedVisitIntakeQuery(command.access, command.access)
    )
    assert [r.source for r in pending.rows] == [command.source]
    assert pending.rows[0].work_order_public_id == "sub-followup-routing"
    db.rollback()
    outcome = owner.open_followup(db, command=command)
    assert outcome.sequence == 1 and outcome.disposition == FollowupDisposition.triage
    workspace = owner.workspace(
        db,
        query=VisitFollowupWorkspaceQuery(
            command.access, command.access, outcome.obligation_id
        ),
    )
    assert workspace.work_order_public_id == "sub-followup-routing"
    db.rollback()
    assert (
        outcome.responsible_team_id == ids[3] and outcome.responsible_person_id is None
    )
    assert (
        owner.pending_failures(
            db, query=FailedVisitIntakeQuery(command.access, command.access)
        ).rows
        == ()
    )
    assert (
        owner.followup_queue(
            db, query=VisitFollowupQueueQuery(command.access, command.access)
        )
        .rows[0]
        .obligation_id
        == outcome.obligation_id
    )


def test_exact_replay_no_second_revision_and_conflicting_key_refused(followup_rig):
    db, command, _ = followup_rig
    first = owner.open_followup(db, command=command)
    again = owner.open_followup(db, command=command)
    assert again.replayed and again.revision_id == first.revision_id
    assert db.scalar(select(func.count()).select_from(FieldVisitFollowupRevision)) == 1
    db.rollback()
    changed = replace(
        command,
        reason="Different review",
        context=replace(command.context, reason="Different review"),
    )
    with pytest.raises(DomainError, match="different reviewed input"):
        owner.open_followup(db, command=changed)


def test_revision_stale_no_revisit_and_pending_issuance(followup_rig):
    db, command, _ = followup_rig
    first = owner.open_followup(db, command=command)
    next_command = decision(command, first)
    second = owner.decide_followup(db, command=next_command)
    assert second.sequence == 2 and second.replacement_decision_id is None
    assert owner.decide_followup(db, command=next_command).replayed
    with pytest.raises(DomainError, match="Reload current evidence"):
        owner.decide_followup(
            db,
            command=replace(
                next_command,
                context=replace(next_command.context, idempotency_key=str(uuid4())),
            ),
        )
    closed = owner.decide_followup(
        db, command=decision(command, second, FollowupDisposition.no_revisit)
    )
    assert closed.sequence == 3
    assert (
        owner.followup_queue(
            db, query=VisitFollowupQueueQuery(command.access, command.access)
        ).rows
        == ()
    )
    assert (
        len(
            owner.followup_queue(
                db,
                query=VisitFollowupQueueQuery(
                    command.access, command.access, include_closed=True
                ),
            ).rows
        )
        == 1
    )


def test_fresh_auth_before_replay(followup_rig):
    db, command, _ = followup_rig
    owner.open_followup(db, command=command)
    with pytest.raises(DomainError, match="Current staff permission"):
        owner.open_followup(
            db,
            command=replace(
                command,
                access=replace(command.access, admitted_permissions=frozenset()),
            ),
        )


def test_no_responsibility_drift_or_partial_receipt(followup_rig):
    db, command, ids = followup_rig
    team = db.get(ServiceTeam, ids[3])
    team.is_active = False
    db.commit()
    with pytest.raises(DomainError, match="active responsible"):
        owner.open_followup(db, command=command)
    assert db.scalar(select(func.count()).select_from(FieldVisitFollowup)) == 0
    assert db.scalar(select(func.count()).select_from(FieldVisitFollowupReceipt)) == 0


def test_atomic_rollback_on_outbox_failure(followup_rig, monkeypatch):
    db, command, _ = followup_rig

    def fail(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(owner, "emit_event", fail)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        owner.open_followup(db, command=command)
    assert db.scalar(select(func.count()).select_from(FieldVisitFollowup)) == 0
    assert db.scalar(select(func.count()).select_from(FieldVisitFollowupRevision)) == 0
    assert db.scalar(select(func.count()).select_from(FieldVisitFollowupReceipt)) == 0


def test_optional_person_requires_exact_current_team_membership(followup_rig):
    from app.models.service_team import ServiceTeamMember

    db, command, ids = followup_rig
    selected = replace(
        command, responsibility=replace(command.responsibility, system_user_id=ids[0])
    )
    with pytest.raises(DomainError, match="active native staff member"):
        owner.open_followup(db, command=selected)
    member = ServiceTeamMember(team_id=ids[3], person_id=ids[1], is_active=True)
    db.add(member)
    db.commit()
    outcome = owner.open_followup(db, command=selected)
    assert outcome.responsible_person_id == ids[1]
    member = db.scalar(
        select(ServiceTeamMember).where(ServiceTeamMember.team_id == ids[3])
    )
    member.is_active = False
    db.commit()
    with pytest.raises(DomainError, match="active native staff member"):
        owner.decide_followup(db, command=decision(selected, outcome))


def test_dual_read_and_same_principal_required_for_pending_intake(followup_rig):
    db, command, _ = followup_rig
    denied = replace(command.access, admitted_permissions=frozenset((READ,)))
    with pytest.raises(DomainError, match="Current staff permission"):
        owner.pending_failures(db, query=FailedVisitIntakeQuery(command.access, denied))


def test_approved_option_delegates_exact_failure_to_completion_owner(
    followup_rig, monkeypatch
):
    from types import SimpleNamespace

    from app.services.field import completion_review
    from app.services.field.completion_review_contracts import (
        CurrentReplacementEvidence,
        ReplacementState,
    )
    from app.services.field.visit_followup_contracts import (
        ApprovedFollowupReplacementQuery,
    )

    db, command, ids = followup_rig
    first = owner.open_followup(db, command=command)
    decision_id = uuid4()
    current = SimpleNamespace(
        decision_id=decision_id,
        generation=3,
        state=ReplacementState.approved,
        replacement_work_order_public_id="sub-replacement",
        replacement_title="Restore rooftop access",
    )
    monkeypatch.setattr(
        completion_review,
        "failed_scope_replacement_workspace",
        lambda db, query: SimpleNamespace(current_decision=current),
    )
    calls = []

    def evidence(db, *, query):
        calls.append(query)
        return CurrentReplacementEvidence(
            decision_id=decision_id,
            source_work_order_id=ids[2],
            replacement_work_order_id=uuid4(),
            source_enrollment_id=uuid4(),
            replacement_enrollment_id=uuid4(),
            generation=3,
        )

    monkeypatch.setattr(completion_review, "current_replacement_evidence", evidence)
    option = owner.approved_replacement_option(
        db,
        query=ApprovedFollowupReplacementQuery(
            command.access, command.access, command.access, first.obligation_id
        ),
    )
    assert option.work_order_public_id == "sub-replacement" and option.generation == 3
    assert calls[0].source_work_order_id == ids[2]
    assert calls[0].failure_basis.event_id == command.source.source_id
    assert calls[0].decision_id == decision_id
    db.rollback()
    linked = replace(
        decision(command, first),
        replacement_decision_id=decision_id,
        replacement_access=command.access,
        expected_replacement_generation=2,
    )
    with pytest.raises(DomainError, match="Approved replacement changed"):
        owner.decide_followup(db, command=linked)
