"""Field lifecycle acceptance on the existing Alembic-migrated PostgreSQL fixture.

The integration package rejects non-PostgreSQL targets. These tests create only
labelled fixture rows; schema preparation and remote execution belong to the
repository integration runner. Object/channel transports are never invoked.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
)
from app.models.dispatch import (
    TechnicianProfile,
    WorkOrderAssignmentQueue,
    WorkOrderAssignmentReceipt,
)
from app.models.field_identity import (
    FieldIdentityReceipt,
    FieldIdentityRevision,
    IdentityState,
)
from app.models.field_job_event import FieldJobEvent
from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentTicketLink,
    OutageIncidentWorkOrderLink,
    OutageScopeRevision,
    OutageScopeRevisionMember,
)
from app.models.notification import CommunicationIntentRecord
from app.models.stored_file import StoredFile
from app.models.subscriber import Address, Subscriber, UserType
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import (
    customer_experience_communications,
    customer_field_identity,
    field_identity,
)
from app.services.bulk_actions import membership_scope_token
from app.services.customer_work_order_selfcare import (
    TechnicianLocationQuery,
    technician_location,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.field.execution_contracts import ApplyFieldTransition, FieldEvent
from app.services.field.job_brief import FieldJobBriefQuery, get_job_brief
from app.services.field.location_tracking import (
    LocationPingCommand,
    LocationRequester,
    LocationSharingCommand,
    field_location_tracking,
)
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import FieldAccessError
from app.services.field_identity_contracts import (
    REVIEW_PERMISSION,
    WRITE_SCOPE,
    IdentityAccess,
    ReviewIdentity,
    SubmitIdentity,
)
from app.services.file_storage import PreparedFileUpload, file_uploads
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.subscriber import _default_reseller_id
from app.services.topology.outage_audience_read import (
    AudienceKnowledge,
    OutageAudienceReadError,
)
from app.services.work_order_assignment_contracts import (
    AssignmentOperation,
    TechnicianAssignmentTarget,
    WorkOrderAssignmentCommand,
)
from app.services.work_order_classification_contracts import ClassifyWorkOrderCommand
from app.services.work_order_commands import work_order_commands
from app.services.work_order_errors import WorkOrderCommandError
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
)


def _context(actor: UUID, *, scope: str = "field:acceptance") -> CommandContext:
    identity = uuid4()
    return CommandContext.system(
        actor=f"user:{actor}",
        scope=scope,
        reason="Labelled field lifecycle PostgreSQL acceptance",
        command_id=identity,
        idempotency_key=str(identity),
    )


def _customer(db: Session) -> Subscriber:
    customer = Subscriber(
        first_name="Acceptance",
        last_name="Customer",
        email=f"field-acceptance-{uuid4()}@example.invalid",
        reseller_id=_default_reseller_id(db),
    )
    db.add(customer)
    db.flush()
    return customer


def _user(db: Session) -> SystemUser:
    user = SystemUser(
        first_name="Acceptance",
        last_name="Staff",
        email=f"field-acceptance-{uuid4()}@example.invalid",
        is_active=True,
        user_type=UserType.system_user,
    )
    db.add(user)
    db.flush()
    return user


@dataclass(frozen=True)
class _Visit:
    user: UUID
    technician: UUID
    subscriber: UUID
    other_subscriber: UUID
    job: UUID
    public_id: str
    revision: datetime


@pytest.fixture
def visit(db_session: Session) -> _Visit:
    user = _user(db_session)
    profile = TechnicianProfile(
        system_user_id=user.id,
        person_id=user.id,
        is_active=True,
    )
    customer, other = _customer(db_session), _customer(db_session)
    job = WorkOrder(
        public_id=f"field-acceptance-{uuid4()}",
        title="Acceptance customer visit",
        subscriber_id=customer.id,
        work_order_kind="customer",
        status="scheduled",
    )
    db_session.add_all([profile, job])
    db_session.flush()
    result = _Visit(
        user.id,
        profile.id,
        customer.id,
        other.id,
        job.id,
        job.public_id,
        job.updated_at,
    )
    db_session.commit()
    return result


def _assignment_access(user_id: UUID) -> PermissionAdmission:
    """Server gate evidence for the labelled active native staff fixture."""
    return PermissionAdmission(
        principal_id=user_id,
        principal_type="system_user",
        admitted_permissions=frozenset({"operations:dispatch:assign"}),
    )


def _assignment_context(user_id: UUID) -> CommandContext:
    # Assignment admission binds the actual UUID actor. Identity review retains
    # its separate user-prefixed actor contract through _context above.
    return replace(
        _context(user_id, scope="operations:dispatch:assign"), actor=str(user_id)
    )


def _assignment(visit: _Visit) -> WorkOrderAssignmentCommand:
    return WorkOrderAssignmentCommand(
        work_order_public_id=visit.public_id,
        access=_assignment_access(visit.user),
        target=TechnicianAssignmentTarget(visit.technician),
    )


def test_assignment_consequence_rollback_replay_and_generation_rotation(
    db_session: Session, visit: _Visit, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = customer_experience_communications.request_assignment_update

    def fail_after_staging(
        db: Session,
        *,
        request: customer_experience_communications.AssignmentCommunicationRequest,
    ) -> None:
        original(db, request=request)
        raise RuntimeError("acceptance consequence failure")

    command, context = _assignment(visit), _assignment_context(visit.user)
    with monkeypatch.context() as patch:
        patch.setattr(
            customer_experience_communications,
            "request_assignment_update",
            fail_after_staging,
        )
        with pytest.raises(RuntimeError, match="acceptance consequence failure"):
            work_order_commands.assign(db_session, command=command, context=context)
    assert db_session.get(WorkOrder, visit.job).status == "scheduled"
    assert (
        db_session.query(WorkOrderAssignmentQueue)
        .filter_by(work_order_mirror_id=visit.job)
        .count()
        == 0
    )
    assert (
        db_session.query(WorkOrderAssignmentReceipt)
        .filter_by(command_id=context.command_id)
        .count()
        == 0
    )
    assert (
        db_session.query(CommunicationIntentRecord)
        .filter_by(dedupe_key=f"work-order-assignment:{context.command_id}")
        .count()
        == 0
    )
    db_session_adapter.release_read_transaction(db_session)
    first = work_order_commands.assign(db_session, command=command, context=context)
    replay = work_order_commands.assign(db_session, command=command, context=context)
    assert first.assignment_generation is not None
    assert (
        replay.replayed and replay.assignment_generation == first.assignment_generation
    )
    assert (
        db_session.query(CommunicationIntentRecord)
        .filter_by(dedupe_key=f"work-order-assignment:{context.command_id}")
        .count()
        == 1
    )
    db_session_adapter.release_read_transaction(db_session)
    second = work_order_commands.assign(
        db_session, command=command, context=_assignment_context(visit.user)
    )
    assert (
        second.assignment_generation is not None
        and second.assignment_generation != first.assignment_generation
    )
    rescheduled = work_order_commands.assign(
        db_session,
        command=replace(
            command,
            operation=AssignmentOperation.reschedule,
            scheduled_start=datetime.now(UTC) + timedelta(days=1),
        ),
        context=_assignment_context(visit.user),
    )
    assert rescheduled.assignment_generation == second.assignment_generation
    assert (
        db_session.query(WorkOrderAssignmentQueue)
        .filter_by(work_order_mirror_id=visit.job, status="assigned")
        .count()
        == 1
    )


def test_stale_offline_generation_cannot_restore_customer_tracking(
    db_session: Session, visit: _Visit
) -> None:
    command = _assignment(visit)
    first = work_order_commands.assign(
        db_session, command=command, context=_assignment_context(visit.user)
    )
    departure = ApplyFieldTransition(
        context=_context(visit.user),
        requester_system_user_id=visit.user,
        public_id=visit.public_id,
        event=FieldEvent.en_route,
        client_event_id=uuid4(),
        assignment_generation=first.assignment_generation,
    )
    field_transitions.apply(db_session, departure)
    second = work_order_commands.assign(
        db_session, command=command, context=_assignment_context(visit.user)
    )
    assert first.assignment_generation != second.assignment_generation
    # Replaying accepted old evidence is stable but must never become current evidence.
    assert field_transitions.apply(db_session, departure).replayed
    for generation in (None, first.assignment_generation):
        with pytest.raises(FieldAccessError) as error:
            field_transitions.apply(
                db_session,
                replace(
                    departure,
                    context=_context(visit.user),
                    client_event_id=uuid4(),
                    assignment_generation=generation,
                ),
            )
        assert error.value.code.endswith("assignment_generation_mismatch")
    requester = LocationRequester(visit.user)
    batch = field_location_tracking.record_batch(
        db_session,
        requester,
        [
            LocationPingCommand(
                latitude=9.1,
                longitude=7.4,
                work_order_public_id=visit.public_id,
                assignment_generation=generation,
            )
            for generation in (
                None,
                first.assignment_generation,
                second.assignment_generation,
            )
        ],
    )
    assert batch.accepted == 1
    assert [issue.code for issue in batch.errors] == [
        "assignment_generation_mismatch",
        "assignment_generation_mismatch",
    ]
    db_session_adapter.release_read_transaction(db_session)
    field_location_tracking.set_sharing(
        db_session, requester, command=LocationSharingCommand(enabled=True)
    )
    query = TechnicianLocationQuery(visit.subscriber, visit.public_id)
    assert technician_location(db_session, query=query).reason == "not_departed"
    db_session_adapter.release_read_transaction(db_session)
    field_transitions.apply(
        db_session,
        replace(
            departure,
            context=_context(visit.user),
            client_event_id=uuid4(),
            assignment_generation=second.assignment_generation,
        ),
    )
    assert technician_location(db_session, query=query).available
    assert not technician_location(
        db_session, query=replace(query, subscriber_id=visit.other_subscriber)
    ).available
    assert (
        db_session.query(FieldJobEvent)
        .filter_by(work_order_mirror_id=visit.job, event="en_route")
        .count()
        == 2
    )
    db_session_adapter.release_read_transaction(db_session)
    field_location_tracking.set_sharing(
        db_session, requester, command=LocationSharingCommand(enabled=False)
    )
    assert technician_location(db_session, query=query).reason == "sharing_off"


def test_classification_is_atomic_and_exact_replay_preserves_revision(
    db_session: Session, visit: _Visit, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import work_order_commands as owner

    command = ClassifyWorkOrderCommand(
        context=_context(visit.user),
        work_order_id=visit.job,
        expected_revision=visit.revision,
        classification=ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose.REPAIR,
            access_technology=WorkOrderAccessTechnology.FIBER,
        ),
    )
    original = owner.emit_event

    def fail_after_event(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("acceptance classification event failure")

    with monkeypatch.context() as patch:
        patch.setattr(owner, "emit_event", fail_after_event)
        with pytest.raises(
            RuntimeError, match="acceptance classification event failure"
        ):
            work_order_commands.classify(db_session, command=command)
    assert db_session.get(WorkOrder, visit.job).purpose is None
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="work_order.classified", entity_id=visit.public_id)
        .count()
        == 0
    )
    db_session_adapter.release_read_transaction(db_session)
    first = work_order_commands.classify(db_session, command=command)
    replay = work_order_commands.classify(db_session, command=command)
    assert replay.replayed and replay.revision == first.revision
    assert replay.recommendation == first.recommendation
    assert first.recommendation.use.value == "recommendation_only"
    with pytest.raises(WorkOrderCommandError) as error:
        work_order_commands.classify(
            db_session, command=replace(command, context=_context(visit.user))
        )
    assert error.value.code == "stale_classification"
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="work_order.classified", entity_id=visit.public_id)
        .count()
        == 1
    )


class PreparedObjectStorage:
    """Synthetic prepared objects exist; object transport is outside this test."""

    def exists(self, key: str) -> bool:
        return True


@pytest.fixture(autouse=True)
def prepared_object_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_uploads, "storage", PreparedObjectStorage())


def _upload(subject: UUID, *, portrait: bool) -> PreparedFileUpload:
    kind = "field_identity_portrait" if portrait else "field_identity_evidence"
    return PreparedFileUpload(
        owner_subscriber_id=None,
        entity_type=kind,
        entity_id=str(subject),
        original_filename="synthetic.png",
        storage_key=f"attachments/{kind}/{subject}/{uuid4()}.png",
        file_size=123,
        content_type="image/png",
        checksum="a" * 64,
        uploaded_by=None,
    )


def test_identity_manual_review_rollback_and_stale_portrait_denial(
    db_session: Session, visit: _Visit, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer = _user(db_session)
    reviewer_id = reviewer.id
    db_session.commit()
    submit = SubmitIdentity(
        _context(visit.user, scope=WRITE_SCOPE),
        IdentityAccess(visit.user),
        visit.user,
        0,
        "Acceptance Engineer",
        "Acceptance Company",
        _upload(visit.user, portrait=True),
        _upload(visit.user, portrait=False),
    )
    original = field_identity.emit_event

    def fail_after_event(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("acceptance identity event failure")

    with monkeypatch.context() as patch:
        patch.setattr(field_identity, "emit_event", fail_after_event)
        with pytest.raises(RuntimeError, match="acceptance identity event failure"):
            field_identity.submit(db_session, command=submit)
    assert (
        db_session.query(FieldIdentityRevision).filter_by(subject_id=visit.user).count()
        == 0
    )
    assert (
        db_session.query(FieldIdentityReceipt).filter_by(subject_id=visit.user).count()
        == 0
    )
    assert (
        db_session.query(StoredFile).filter_by(entity_id=str(visit.user)).count() == 0
    )
    db_session_adapter.release_read_transaction(db_session)
    pending = field_identity.submit(db_session, command=submit)
    assert field_identity.submit(db_session, command=submit) == pending
    assert (
        db_session.query(FieldIdentityRevision).filter_by(subject_id=visit.user).count()
        == 1
    )
    assert (
        db_session.query(FieldIdentityReceipt).filter_by(subject_id=visit.user).count()
        == 1
    )
    assert (
        db_session.query(StoredFile).filter_by(entity_id=str(visit.user)).count() == 2
    )
    db_session_adapter.release_read_transaction(db_session)
    denied = ReviewIdentity(
        _context(visit.user, scope=WRITE_SCOPE),
        IdentityAccess(visit.user, frozenset({REVIEW_PERMISSION})),
        visit.user,
        1,
        1,
        IdentityState.reviewed,
    )
    with pytest.raises(field_identity.FieldIdentityError):
        field_identity.review(db_session, command=denied)
    review = replace(
        denied,
        context=_context(reviewer_id, scope=WRITE_SCOPE),
        access=IdentityAccess(reviewer_id, frozenset({REVIEW_PERMISSION})),
    )
    approved = field_identity.review(db_session, command=review)
    assert field_identity.review(db_session, command=review) == approved
    assert approved.state == IdentityState.reviewed and approved.version == 2
    first = work_order_commands.assign(
        db_session, command=_assignment(visit), context=_assignment_context(visit.user)
    )
    query = customer_field_identity.CustomerFieldIdentityQuery(
        visit.subscriber, visit.job
    )
    card = customer_field_identity.card(db_session, query=query).card
    assert (
        card is not None and card.assignment_generation == first.assignment_generation
    )
    with pytest.raises(customer_field_identity.CustomerFieldIdentityError):
        customer_field_identity.card(
            db_session, query=replace(query, subscriber_id=visit.other_subscriber)
        )
    db_session_adapter.release_read_transaction(db_session)
    second = work_order_commands.assign(
        db_session, command=_assignment(visit), context=_assignment_context(visit.user)
    )
    assert second.assignment_generation != first.assignment_generation
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda *args, **kwargs: pytest.fail(
            "Stale private portrait reached object transport"
        ),
    )
    assert first.assignment_generation is not None
    with pytest.raises(customer_field_identity.CustomerFieldIdentityError):
        customer_field_identity.portrait(
            db_session,
            query=customer_field_identity.CustomerFieldPortraitQuery(
                visit.subscriber,
                visit.job,
                first.assignment_generation,
                card.portrait_revision,
            ),
        )


def _revision(
    db: Session,
    incident: OutageIncident,
    target: UUID | None,
    ids: tuple[UUID, ...],
    *,
    sequence: int = 1,
) -> OutageScopeRevision:
    scope = "fdh-cabinet" if target else "unresolved"
    revision = OutageScopeRevision(
        incident_id=incident.id,
        sequence=sequence,
        effective_at=datetime.now(UTC),
        reason="confirmed",
        new_scope_type=scope,
        new_scope_id=target,
        membership_token=membership_scope_token(
            f"{scope}:{target}", [str(sid) for sid in ids]
        ),
        member_count=len(ids),
        entered_count=len(ids),
        left_count=0,
    )
    db.add(revision)
    db.flush()
    db.add_all(
        [
            OutageScopeRevisionMember(
                revision_id=revision.id, subscription_id=sid, membership="entered"
            )
            for sid in ids
        ]
    )
    db.flush()
    return revision


def test_brief_exact_audience_pagination_and_cross_customer_address_privacy(
    db_session: Session, visit: _Visit
) -> None:
    cabinet = FdhCabinet(
        name="Acceptance shared cabinet",
        code=f"acceptance-{uuid4()}",
        latitude=9.1,
        longitude=7.4,
    )
    offer = CatalogOffer(
        name=f"Acceptance offer {uuid4()}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    outsider = _customer(db_session)
    db_session.add_all([cabinet, offer])
    db_session.flush()
    private_address = Address(
        subscriber_id=outsider.id, address_line1="Synthetic unrelated address"
    )
    db_session.add(private_address)
    db_session.flush()
    ids = tuple(sorted((uuid4(), uuid4()), key=str))
    db_session.add_all(
        [
            Subscription(
                id=sid,
                subscriber_id=visit.subscriber if i == 0 else visit.other_subscriber,
                offer_id=offer.id,
                service_address_id=private_address.id if i == 0 else None,
            )
            for i, sid in enumerate(ids)
        ]
    )
    infrastructure_ticket = Ticket(
        title="Acceptance shared feeder infrastructure repair", status="open"
    )
    db_session.add(infrastructure_ticket)
    db_session.flush()
    job = WorkOrder(
        origin_ticket_id=infrastructure_ticket.id,
        public_id=f"field-shared-acceptance-{uuid4()}",
        title="Acceptance shared feeder repair",
        work_order_kind="infrastructure",
        status="scheduled",
    )
    incident = OutageIncident(fdh_cabinet_id=cabinet.id)
    db_session.add_all([job, incident])
    db_session.flush()
    revision = _revision(db_session, incident, cabinet.id, ids)
    db_session.add(
        OutageIncidentTicketLink(
            incident_id=incident.id,
            ticket_id=infrastructure_ticket.id,
            role="infrastructure",
            source="operator",
            linked_by=str(visit.user),
            scope_revision_sequence=revision.sequence,
        )
    )
    db_session.flush()
    link = OutageIncidentWorkOrderLink(
        incident_id=incident.id,
        work_order_id=job.id,
        scope_revision_id=revision.id,
        scope_revision_sequence=revision.sequence,
        membership_token=revision.membership_token,
        target_type="fdh-cabinet",
        target_id=cabinet.id,
        idempotency_key=str(uuid4()),
        command_fingerprint="acceptance-fixture",
    )
    db_session.add(link)
    public_id = job.public_id
    db_session.commit()
    work_order_commands.assign(
        db_session,
        command=WorkOrderAssignmentCommand(
            work_order_public_id=public_id,
            access=_assignment_access(visit.user),
            target=TechnicianAssignmentTarget(visit.technician),
        ),
        context=_assignment_context(visit.user),
    )
    query = FieldJobBriefQuery(visit.user, public_id, limit=1)
    first = get_job_brief(db_session, query)
    assert first.affected_subscription_count == 2
    assert first.affected_customers[0].subscription_id == ids[0]
    assert first.affected_customers[0].service_address is None
    assert (
        first.outage_audience is not None and first.outage_audience.current is not None
    )
    second = get_job_brief(
        db_session,
        replace(
            query,
            audience_link_id=first.outage_audience.link_id,
            expected_revision_id=first.outage_audience.current.id,
            after_subscription_id=first.outage_audience.next_after_subscription_id,
        ),
    )
    assert tuple(
        customer.subscription_id for customer in second.affected_customers
    ) == (ids[1],)
    assert (
        second.outage_audience is not None
        and second.outage_audience.next_after_subscription_id is None
    )
    _revision(db_session, incident, None, (), sequence=2)
    db_session.commit()
    with pytest.raises(OutageAudienceReadError) as error:
        get_job_brief(
            db_session,
            replace(
                query,
                expected_revision_id=first.outage_audience.current.id,
                after_subscription_id=ids[0],
            ),
        )
    assert error.value.code.endswith("revision_changed")
    unknown = get_job_brief(db_session, query)
    assert (
        unknown.affected_subscription_count is None and unknown.affected_customers == ()
    )
    assert (
        unknown.outage_audience is not None
        and unknown.outage_audience.knowledge == AudienceKnowledge.unknown
    )
    stranger = _user(db_session)
    with pytest.raises(FieldAccessError):
        get_job_brief(db_session, replace(query, requester_system_user_id=stranger.id))
