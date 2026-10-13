"""Exact actual-issuance fixture; existing native acceptance kernel remains shared."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_job_event import FieldJobEvent
from app.models.rbac import Permission, SystemUserPermission
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services.field import completion_review as completion
from app.services.field.completion_review_contracts import (
    READ,
    WRITE,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionProvenance,
    EnrollCompletionReview,
    IssuedCompletionCriterion,
    PublishCompletionReviewPolicy,
)
from app.services.permission_admission import PermissionAdmission
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    WorkOrderAssignmentCommand,
)
from app.services.work_order_commands import work_order_commands
from tests.integration.test_support_completion_gate_acceptance import NativeJob, context
from tests.staff_identity_fixtures import add_bound_staff_user


def prepare_issued_job(engine: Engine, *, work_order_id: UUID) -> NativeJob:
    with Session(engine) as db:
        reviewer, _ = add_bound_staff_user(db)
        technician, _ = add_bound_staff_user(db)
        profile = TechnicianProfile(
            person_id=technician.id, system_user_id=technician.id, is_active=True
        )
        db.add(profile)
        required = {READ, WRITE, "operations:dispatch:assign"}
        permissions = db.scalars(
            select(Permission).where(
                Permission.key.in_(required), Permission.is_active.is_(True)
            )
        ).all()
        assert {permission.key for permission in permissions} == required
        for permission in permissions:
            db.add(
                SystemUserPermission(
                    system_user_id=reviewer.id, permission_id=permission.id
                )
            )
        work = db.get(WorkOrder, work_order_id)
        assert (
            work is not None
            and work.subscriber_id is None
            and work.work_order_kind == "infrastructure"
        )
        ticket = db.get(Ticket, work.origin_ticket_id)
        assert ticket is not None
        work_id, public_id, revision = work.id, work.public_id, work.updated_at
        profile_id, reviewer_id, technician_id, ticket_id = (
            profile.id,
            reviewer.id,
            technician.id,
            ticket.id,
        )
        db.commit()
    access = PermissionAdmission(reviewer_id, "system_user", frozenset(required))
    with Session(engine) as db:
        assigned = work_order_commands.assign(
            db,
            command=WorkOrderAssignmentCommand(
                access=access,
                work_order_public_id=public_id,
                target=TechnicianAssignmentTarget(profile_id),
                expected_revision=revision,
                reason="Assign synthetic visit",
            ),
            context=context(reviewer_id, "operations:dispatch:assign"),
        )
    assert assigned.assignment_generation is not None
    with Session(engine) as db:
        row = db.get(WorkOrder, work_id)
        assert row is not None
        row.status = "completed"
        now = datetime.now(UTC)
        event = FieldJobEvent(
            work_order_mirror_id=work_id,
            author_technician_id=profile_id,
            person_id=technician_id,
            system_user_id=technician_id,
            event="complete",
            previous_status="in_progress",
            new_status="completed",
            occurred_at=now,
            received_at=now,
            assignment_generation=assigned.assignment_generation,
            client_event_id=uuid4(),
        )
        db.add(event)
        db.flush()
        event_id = event.id
        db.commit()
    scope = CompletionPolicyScope(
        kind=CompletionPolicyScopeKind.work_order, work_order_public_id=public_id
    )
    with Session(engine) as db:
        completion.publish_policy(
            db,
            command=PublishCompletionReviewPolicy(
                context=context(reviewer_id, WRITE),
                access=access,
                scope=scope,
                expected_generation=0,
                independent_reviewer_required=True,
            ),
        )
    return NativeJob(
        ticket_id=ticket_id,
        work_order_id=work_id,
        public_id=public_id,
        reviewer_id=reviewer_id,
        technician_id=technician_id,
        access=access,
        assignment_generation=assigned.assignment_generation,
        completed_event_id=event_id,
        enrollment=EnrollCompletionReview(
            context=context(reviewer_id, WRITE),
            access=access,
            work_order_public_id=public_id,
            provenance=CompletionProvenance.native_work_order,
            policy_scope=scope,
            criteria=(
                IssuedCompletionCriterion(
                    id=uuid4(),
                    order=0,
                    statement="Repair verified",
                    verification_method="Measure service",
                ),
            ),
        ),
    )


def active_shared(engine, command):
    """Seed an explicit persisted audience, then issue through canonical owner."""
    from app.models.network_monitoring import (
        NetworkDevice,
        OutageIncident,
        OutageIncidentTicketLink,
        OutageScopeRevision,
        OutageScopeRevisionMember,
    )
    from app.models.service_team import ServiceTeam, ServiceTeamMember
    from app.models.system_user import SystemUser
    from app.services.topology import outage

    with Session(engine) as db:
        outage.link_complaint_ticket(db, command=command)
    with Session(engine) as db:
        team = ServiceTeam(name=f"Synthetic outage crew {uuid4()}", is_active=True)
        target = NetworkDevice(name=f"Synthetic shared node {uuid4()}")
        db.add_all([team, target])
        db.flush()
        actor = db.get(SystemUser, command.support_access.principal_id)
        db.add(
            ServiceTeamMember(
                team_id=team.id, person_id=actor.person_party_id, is_active=True
            )
        )
        infra = Ticket(
            title="Actual shared feeder repair", service_team_id=team.id, status="open"
        )
        db.add(infra)
        db.flush()
        db.add(
            OutageIncidentTicketLink(
                incident_id=command.incident_id,
                ticket_id=infra.id,
                role="infrastructure",
                source="operator",
            )
        )
        incident = db.get(OutageIncident, command.incident_id)
        incident.root_node_id = target.id
        revision = OutageScopeRevision(
            incident_id=incident.id,
            sequence=2,
            effective_at=datetime.now(UTC),
            reason="rerooted",
            new_scope_type="node",
            new_scope_id=target.id,
            membership_token="b" * 64,
            member_count=1,
        )
        db.add(revision)
        db.flush()
        db.add(
            OutageScopeRevisionMember(
                revision_id=revision.id,
                subscription_id=command.subscription_id,
                membership="retained",
            )
        )
        db.commit()
        return infra.id


def issue_actual(engine, command):
    from app.schemas.network import InfrastructureWorkOrderIssueRequest
    from app.services.topology.outage_work_order_handoff import (
        ISSUE_SCOPE,
        REQUIRED_PERMISSIONS,
        HandoffActorType,
        OutageWorkOrderIssueCommand,
        issue_work_order,
    )

    with Session(engine) as db:
        outcome = issue_work_order(
            db,
            OutageWorkOrderIssueCommand(
                incident_id=command.incident_id,
                request=InfrastructureWorkOrderIssueRequest(
                    title="Canonical issued shared repair",
                    reason="Synthetic exact reviewed outage field scope",
                    requires_as_built_evidence=False,
                ),
                actor_id=command.support_access.principal_id,
                actor_type=HandoffActorType.SYSTEM_USER,
                permissions=REQUIRED_PERMISSIONS,
                context=context(command.support_access.principal_id, ISSUE_SCOPE),
            ),
        )
        return outcome.work_order.id


def restore(engine, command):
    from app.services.topology import outage

    with Session(engine) as db:
        assert outage.resolve_outage(db, command.incident_id) is not None
        db.commit()


def next_audience(engine, command, *, retain_source=True, new_target=False):
    from app.models.catalog import Subscription
    from app.models.network_monitoring import (
        NetworkDevice,
        OutageIncident,
        OutageScopeRevision,
        OutageScopeRevisionMember,
    )
    from app.services.bulk_actions import membership_scope_token

    with Session(engine) as db:
        incident = db.scalar(
            select(OutageIncident)
            .where(OutageIncident.id == command.incident_id)
            .with_for_update()
        )
        previous = db.scalar(
            select(OutageScopeRevision)
            .where(OutageScopeRevision.incident_id == incident.id)
            .order_by(OutageScopeRevision.sequence.desc())
            .limit(1)
        )
        service = db.get(Subscription, command.subscription_id)
        other = Subscription(
            subscriber_id=service.subscriber_id, offer_id=service.offer_id
        )
        db.add(other)
        db.flush()
        if new_target:
            target = NetworkDevice(name=f"Different exact node {uuid4()}")
            db.add(target)
            db.flush()
            incident.root_node_id = target.id
        members = {str(other.id)} | (
            {str(command.subscription_id)} if retain_source else set()
        )
        revision = OutageScopeRevision(
            incident_id=incident.id,
            sequence=previous.sequence + 1,
            effective_at=datetime.now(UTC),
            reason="audience_drift",
            new_scope_type="node",
            new_scope_id=incident.root_node_id,
            membership_token=membership_scope_token(
                f"node:{incident.root_node_id}", sorted(members)
            ),
            member_count=len(members),
        )
        db.add(revision)
        db.flush()
        for member in members:
            db.add(
                OutageScopeRevisionMember(
                    revision_id=revision.id,
                    subscription_id=UUID(member),
                    membership="retained",
                )
            )
        db.commit()
        return revision.id
