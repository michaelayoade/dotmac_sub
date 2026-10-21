"""Non-authoritative fast customer identity disclosure tests using typed queries."""

from dataclasses import asdict
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from PIL import Image
from sqlalchemy import update

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_identity import FieldIdentityRevision, IdentityState
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import customer_field_identity as owner
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult


@pytest.fixture
def assigned(db_session):
    customer, other_customer, subject, reviewer, technician, job, generation = (
        uuid4() for _ in range(7)
    )
    for sid in (customer, other_customer):
        db_session.add(
            Subscriber(
                id=sid,
                first_name="Synthetic",
                last_name="Customer",
                email=f"{sid}@example.invalid",
            )
        )
    for sid in (subject, reviewer):
        db_session.add(
            SystemUser(
                id=sid,
                first_name="Synthetic",
                last_name="Staff",
                email=f"{sid}@example.invalid",
                is_active=True,
            )
        )
    db_session.flush()
    db_session.add(
        TechnicianProfile(
            id=technician, person_id=uuid4(), system_user_id=subject, is_active=True
        )
    )
    db_session.add(
        WorkOrder(
            id=job,
            public_id=f"TEST-{job}",
            subscriber_id=customer,
            work_order_kind="customer",
            title="Synthetic visit",
            is_active=True,
        )
    )
    db_session.flush()
    queue = WorkOrderAssignmentQueue(
        id=uuid4(),
        work_order_mirror_id=job,
        status="assigned",
        assigned_technician_id=technician,
        assignment_generation=generation,
        assignment_effective_at=datetime.now(UTC),
    )
    db_session.add(queue)
    file_ids = []
    for kind in ("field_identity_portrait", "field_identity_evidence"):
        fid = uuid4()
        file_ids.append(fid)
        db_session.add(
            StoredFile(
                id=fid,
                entity_type=kind,
                entity_id=str(subject),
                original_filename="synthetic.png",
                storage_key_or_relative_path="attachments/private/synthetic.png",
                file_size=3,
                content_type="image/png",
                storage_provider="s3",
                is_deleted=False,
            )
        )
    db_session.flush()
    identity = FieldIdentityRevision(
        subject_id=subject,
        revision=1,
        version=2,
        state="reviewed",
        display_name="Reviewed Engineer",
        company_name="Reviewed Company",
        portrait_file_id=file_ids[0],
        evidence_file_id=file_ids[1],
        reviewer_id=reviewer,
        reviewed_at=datetime.now(UTC),
        review_reason="Synthetic evidence reviewed",
    )
    db_session.add(identity)
    db_session.commit()
    return (
        owner.CustomerFieldIdentityQuery(customer, job),
        other_customer,
        subject,
        technician,
        queue.id,
        generation,
        identity.id,
    )


def test_customer_card_uses_approved_person_without_private_ids(db_session, assigned):
    query, *_ = assigned
    result = owner.card(db_session, query=query)
    assert result.card and result.card.display_name == "Reviewed Engineer"
    assert result.card.verification_state == IdentityState.reviewed
    assert result.unavailable_reason is None
    fields = asdict(result.card)
    assert not {
        "subject_id",
        "portrait_file_id",
        "evidence_file_id",
        "review_reason",
    }.intersection(fields)


def test_foreign_customer_card_and_portrait_never_stream(
    db_session, assigned, monkeypatch
):
    query, other, _, _, _, generation, _ = assigned
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda *args: pytest.fail("Unauthorized portrait stream"),
    )
    with pytest.raises(owner.CustomerFieldIdentityError) as exc:
        owner.card(
            db_session,
            query=owner.CustomerFieldIdentityQuery(other, query.work_order_id),
        )
    assert exc.value.code.endswith("not_found")
    with pytest.raises(owner.CustomerFieldIdentityError):
        owner.portrait(
            db_session,
            query=owner.CustomerFieldPortraitQuery(
                other, query.work_order_id, generation, 1
            ),
        )


def test_portrait_stream_only_after_current_generation_and_revision(
    db_session, assigned, monkeypatch
):
    query, _, _, _, queue_id, generation, _ = assigned
    encoded = BytesIO()
    Image.new("RGB", (2, 2), "red").save(encoded, format="PNG")
    png = encoded.getvalue()
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda file: StreamResult(
            chunks=iter([png]), content_type="image/png", content_length=len(png)
        ),
    )
    pq = owner.CustomerFieldPortraitQuery(
        query.subscriber_id, query.work_order_id, generation, 1
    )
    assert b"".join(owner.portrait(db_session, query=pq).chunks) == png
    queue = db_session.get(WorkOrderAssignmentQueue, queue_id)
    queue.assignment_generation = uuid4()
    db_session.commit()
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda *args: pytest.fail("Stale portrait streamed"),
    )
    with pytest.raises(owner.CustomerFieldIdentityError):
        owner.portrait(db_session, query=pq)


@pytest.mark.parametrize("state", ["pending", "rejected", "revoked"])
def test_unapproved_identity_never_disclosed(db_session, assigned, monkeypatch, state):
    query, _, _, _, _, generation, identity_id = assigned
    identity = db_session.get(FieldIdentityRevision, identity_id)
    identity.state = state
    if state == "pending":
        identity.reviewer_id = None
    db_session.commit()
    assert (
        owner.card(db_session, query=query).unavailable_reason
        == owner.IdentityUnavailableReason.identity_not_approved
    )
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda *args: pytest.fail("Unapproved portrait streamed"),
    )
    with pytest.raises(owner.CustomerFieldIdentityError):
        owner.portrait(
            db_session,
            query=owner.CustomerFieldPortraitQuery(
                query.subscriber_id, query.work_order_id, generation, 1
            ),
        )


@pytest.mark.parametrize(
    "target", ["technician", "subject", "binding", "generation", "queue"]
)
def test_current_assignment_inputs_rechecked(db_session, assigned, target):
    query, _, subject, technician, queue_id, _, _ = assigned
    if target == "technician":
        db_session.get(TechnicianProfile, technician).is_active = False
    elif target == "subject":
        db_session.get(SystemUser, subject).is_active = False
    elif target == "binding":
        db_session.get(TechnicianProfile, technician).system_user_id = None
    elif target == "generation":
        db_session.get(WorkOrderAssignmentQueue, queue_id).assignment_generation = None
    else:
        db_session.get(WorkOrderAssignmentQueue, queue_id).status = "queued"
    db_session.commit()
    assert owner.card(db_session, query=query).card is None


def test_vendor_organisation_never_selects_owner_as_crew(
    db_session, assigned, monkeypatch
):
    from app.services.work_order_assignment_contracts import CurrentWorkOrderAssignment

    query, _, _, _, queue_id, generation, _ = assigned
    assignment = CurrentWorkOrderAssignment(
        queue_id, None, uuid4(), generation, datetime.now(UTC)
    )
    monkeypatch.setattr(
        owner.work_order_commands,
        "current_assignment",
        lambda db, *, query: assignment,
    )
    monkeypatch.setattr(
        owner.field_identity,
        "approved_customer_card",
        lambda *args, **kwargs: pytest.fail("Vendor owner looked up as crew"),
    )
    result = owner.card(db_session, query=query)
    assert (
        result.card is None
        and result.unavailable_reason
        == owner.IdentityUnavailableReason.actual_crew_not_selected
    )


def test_adapter_requires_customer_and_marks_card_no_store(db_session, assigned):
    from app.api.customer_field_identity import _subscriber_id, field_identity_card

    query, *_ = assigned
    with pytest.raises(HTTPException):
        _subscriber_id(
            {"principal_type": "system_user", "subscriber_id": str(query.subscriber_id)}
        )
    response = Response()
    result = field_identity_card(
        query.work_order_id,
        response,
        auth={
            "principal_type": "subscriber",
            "subscriber_id": str(query.subscriber_id),
        },
        db=db_session,
    )
    assert result.card and response.headers["Cache-Control"] == "private, no-store"


def test_replaced_approved_portrait_requires_current_revision(
    db_session, assigned, monkeypatch
):
    query, _, subject, _, _, generation, identity_id = assigned
    previous = db_session.get(FieldIdentityRevision, identity_id)
    replacement = FieldIdentityRevision(
        subject_id=subject,
        revision=2,
        version=2,
        state="reviewed",
        display_name="Reviewed replacement",
        company_name=previous.company_name,
        portrait_file_id=previous.portrait_file_id,
        evidence_file_id=previous.evidence_file_id,
        reviewer_id=previous.reviewer_id,
        reviewed_at=datetime.now(UTC),
        review_reason="Synthetic replacement reviewed",
    )
    db_session.add(replacement)
    db_session.commit()
    result = owner.card(db_session, query=query)
    assert result.card and result.card.portrait_revision == 2
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda *args: pytest.fail("Superseded portrait streamed"),
    )
    with pytest.raises(owner.CustomerFieldIdentityError):
        owner.portrait(
            db_session,
            query=owner.CustomerFieldPortraitQuery(
                query.subscriber_id, query.work_order_id, generation, 1
            ),
        )


def test_identity_shared_locks_follow_transition_subject_before_visit_order(
    db_session, assigned, monkeypatch
):
    query, *_ = assigned
    scalar = db_session.scalar
    locks = []

    def observe(statement, *args, **kwargs):
        if statement._for_update_arg is not None:
            locks.append(statement.column_descriptions[0]["entity"])
            assert statement.get_execution_options()["populate_existing"] is True
        return scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", observe)
    assert owner.card(db_session, query=query).card is not None
    assert locks == [SystemUser, TechnicianProfile, WorkOrder]


@pytest.mark.parametrize("race", ["generation", "binding", "customer"])
def test_snapshot_change_after_discovery_fails_closed_with_stale_orm_loaded(
    db_session, assigned, monkeypatch, race
):
    query, other, _, technician_id, queue_id, generation, _ = assigned
    # Keep stale ORM instances alive: post-wait queries must refresh them.
    stale_queue = db_session.get(WorkOrderAssignmentQueue, queue_id)
    stale_profile = db_session.get(TechnicianProfile, technician_id)
    stale_order = db_session.get(WorkOrder, query.work_order_id)
    scalar = db_session.scalar
    locks = []
    changed = False

    def mutate_before_lock(statement, *args, **kwargs):
        nonlocal changed
        if statement._for_update_arg is not None:
            entity = statement.column_descriptions[0]["entity"]
            locks.append(entity)
            if not changed and entity is SystemUser:
                changed = True
                if race == "generation":
                    change = (
                        update(WorkOrderAssignmentQueue)
                        .where(WorkOrderAssignmentQueue.id == queue_id)
                        .values(assignment_generation=uuid4())
                    )
                elif race == "binding":
                    change = (
                        update(TechnicianProfile)
                        .where(TechnicianProfile.id == technician_id)
                        .values(system_user_id=None)
                    )
                else:
                    change = (
                        update(WorkOrder)
                        .where(WorkOrder.id == query.work_order_id)
                        .values(subscriber_id=other)
                    )
                db_session.execute(change.execution_options(synchronize_session=False))
        return scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", mutate_before_lock)
    monkeypatch.setattr(
        owner.field_identity,
        "approved_customer_card",
        lambda *a, **k: pytest.fail("Changed candidate disclosed"),
    )
    if race == "customer":
        with pytest.raises(owner.CustomerFieldIdentityError):
            owner.card(db_session, query=query)
        assert stale_order.subscriber_id == other
    else:
        assert owner.card(db_session, query=query).card is None
        if race == "generation":
            assert stale_queue.assignment_generation != generation
        else:
            assert stale_profile.system_user_id is None
    assert changed
    assert locks == [SystemUser, TechnicianProfile, WorkOrder]
