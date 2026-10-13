from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_job_event import FieldJobEvent
from app.models.field_location import FieldTechLocationPing, FieldTechPresence
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services import customer_work_order_selfcare
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _customer(db_session) -> Subscriber:
    row = Subscriber(
        first_name="Selfcare",
        last_name="Customer",
        email=f"selfcare-{uuid4().hex}@example.com",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _technician(db_session) -> TechnicianProfile:
    user = SystemUser(
        first_name="Field",
        last_name="Engineer",
        display_name="Field Engineer",
        email=f"field-{uuid4().hex}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        title="Field engineer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _visit(db_session, customer: Subscriber, *, status: str) -> WorkOrder:
    row = WorkOrder(
        public_id=f"sub-{uuid4().hex}",
        subscriber_id=customer.id,
        title="Customer field visit",
        status=status,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _location_ping(
    technician: TechnicianProfile,
    visit: WorkOrder,
    *,
    latitude: float,
    longitude: float,
    captured_at: datetime,
) -> FieldTechLocationPing:
    return FieldTechLocationPing(
        technician_id=technician.id,
        person_id=technician.person_id,
        crm_work_order_id=visit.public_id,
        latitude=latitude,
        longitude=longitude,
        captured_at=captured_at,
        received_at=datetime.now(UTC),
        source="mobile",
    )


def _departure(
    technician: TechnicianProfile, visit: WorkOrder, *, generation: UUID | None = None
) -> FieldJobEvent:
    return FieldJobEvent(
        work_order_mirror_id=visit.id,
        author_technician_id=technician.id,
        person_id=technician.person_id,
        system_user_id=technician.system_user_id,
        event="en_route",
        previous_status="scheduled",
        new_status="dispatched",
        occurred_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        client_event_id=uuid4(),
        assignment_generation=generation,
    )


def test_location_reads_native_assignment_presence_and_public_identity(db_session):
    customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    db_session.add_all(
        [
            _departure(technician, visit),
            WorkOrderAssignmentQueue(
                work_order_mirror_id=visit.id,
                status="assigned",
                assigned_technician_id=technician.id,
            ),
            FieldTechPresence(
                technician_id=technician.id,
                person_id=technician.person_id,
                status="busy",
                location_sharing_enabled=True,
                last_latitude=9.0765,
                last_longitude=7.3986,
                last_location_at=datetime.now(UTC),
            ),
            _location_ping(
                technician,
                visit,
                latitude=9.0765,
                longitude=7.3986,
                captured_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    result = customer_work_order_selfcare.technician_location(
        db_session,
        query=customer_work_order_selfcare.TechnicianLocationQuery(
            customer.id, visit.public_id
        ),
    )

    assert result.available is True
    assert result.work_order_id == visit.public_id
    assert result.latitude == 9.0765
    assert result.updated_at is not None


def test_location_hides_stale_presence(db_session):
    customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    db_session.add_all(
        [
            _departure(technician, visit),
            WorkOrderAssignmentQueue(
                work_order_mirror_id=visit.id,
                status="assigned",
                assigned_technician_id=technician.id,
            ),
            FieldTechPresence(
                technician_id=technician.id,
                person_id=technician.person_id,
                status="busy",
                location_sharing_enabled=True,
                last_latitude=9.0765,
                last_longitude=7.3986,
                last_location_at=datetime.now(UTC) - timedelta(minutes=3),
            ),
            _location_ping(
                technician,
                visit,
                latitude=9.0765,
                longitude=7.3986,
                captured_at=datetime.now(UTC) - timedelta(minutes=3),
            ),
        ]
    )
    db_session.commit()

    result = customer_work_order_selfcare.technician_location(
        db_session,
        query=customer_work_order_selfcare.TechnicianLocationQuery(
            customer.id, visit.public_id
        ),
    )

    assert result.available is False
    assert result.reason == "stale"
    assert result.latitude is None
    assert result.longitude is None


def test_location_does_not_leak_latest_fix_from_another_work_order(db_session):
    customer = _customer(db_session)
    other_customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    other_visit = _visit(db_session, other_customer, status="in_progress")
    db_session.add_all(
        [
            _departure(technician, visit),
            WorkOrderAssignmentQueue(
                work_order_mirror_id=visit.id,
                status="assigned",
                assigned_technician_id=technician.id,
            ),
            FieldTechPresence(
                technician_id=technician.id,
                person_id=technician.person_id,
                status="busy",
                location_sharing_enabled=True,
                last_latitude=6.5244,
                last_longitude=3.3792,
                last_location_at=datetime.now(UTC),
            ),
            _location_ping(
                technician,
                other_visit,
                latitude=6.5244,
                longitude=3.3792,
                captured_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    result = customer_work_order_selfcare.technician_location(
        db_session,
        query=customer_work_order_selfcare.TechnicianLocationQuery(
            customer.id, visit.public_id
        ),
    )

    assert result.available is False
    assert result.reason == "no_fix"
    assert result.latitude is None
    assert result.longitude is None


def test_rating_is_native_audited_and_idempotent(db_session):
    customer = _customer(db_session)
    visit = _visit(db_session, customer, status="completed")
    db_session.commit()

    technician = _technician(db_session)
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=visit.id,
            status="assigned",
            assigned_technician_id=technician.id,
        )
    )
    db_session.commit()
    first = _rate(
        db_session, customer.id, visit.public_id, rating=5, comment="Excellent visit"
    )
    replay = _rate(
        db_session, customer.id, visit.public_id, rating=1, comment="Must not overwrite"
    )

    db_session.refresh(visit)
    assert first.rating == 5
    assert first.work_order_id == visit.public_id
    assert replay.already_rated is True
    assert replay.rating == 5
    assert visit.technician_rating == 5
    assert visit.metadata_["technician_rating"]["source"] == "customer_selfcare"
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order_technician_rated")
        .filter(AuditEvent.entity_id == str(visit.id))
        .count()
        == 1
    )


def test_rating_rejects_unfinished_or_unowned_visit(db_session):
    customer = _customer(db_session)
    other = _customer(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    db_session.commit()

    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError,
        match="work_order_not_completed",
    ):
        _rate(db_session, customer.id, visit.public_id, rating=4)
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError, match="work_order_not_found"
    ):
        _rate(db_session, other.id, visit.public_id, rating=4)


@pytest.mark.parametrize(
    "status", ["dispatched", "in_progress", "paused", "completed", "canceled"]
)
def test_tracking_requires_departure_on_current_generation_until_terminal(
    db_session, status
):
    customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status=status)
    generation = uuid4()
    old_generation = uuid4()
    assignment = WorkOrderAssignmentQueue(
        work_order_mirror_id=visit.id,
        status="assigned",
        assigned_technician_id=technician.id,
        assignment_generation=generation,
        assignment_effective_at=datetime.now(UTC),
    )
    presence = FieldTechPresence(
        technician_id=technician.id,
        person_id=technician.person_id,
        status="busy",
        location_sharing_enabled=True,
    )
    old_ping = _location_ping(
        technician, visit, latitude=9.0, longitude=7.0, captured_at=datetime.now(UTC)
    )
    old_ping.assignment_generation = old_generation
    db_session.add_all(
        [
            assignment,
            presence,
            _departure(technician, visit, generation=old_generation),
            old_ping,
        ]
    )
    db_session.commit()
    query = customer_work_order_selfcare.TechnicianLocationQuery(
        customer.id, visit.public_id
    )
    hidden = customer_work_order_selfcare.technician_location(db_session, query=query)
    assert not hidden.available
    assert hidden.latitude is None
    db_session.add(_departure(technician, visit, generation=generation))
    db_session.flush()
    still_hidden = customer_work_order_selfcare.technician_location(
        db_session, query=query
    )
    assert not still_hidden.available
    ping = _location_ping(
        technician, visit, latitude=9.1, longitude=7.1, captured_at=datetime.now(UTC)
    )
    ping.assignment_generation = generation
    db_session.add(ping)
    db_session.commit()
    visible = customer_work_order_selfcare.technician_location(db_session, query=query)
    assert visible.available is (status in {"dispatched", "in_progress", "paused"})
    if visible.available:
        assert visible.latitude == 9.1
    presence.location_sharing_enabled = False
    db_session.commit()
    sharing_off = customer_work_order_selfcare.technician_location(
        db_session, query=query
    )
    assert not sharing_off.available
    assert sharing_off.latitude is None


def test_location_query_does_not_expose_another_subscribers_departure(db_session):
    customer = _customer(db_session)
    other = _customer(db_session)
    visit = _visit(db_session, customer, status="dispatched")
    result = customer_work_order_selfcare.technician_location(
        db_session,
        query=customer_work_order_selfcare.TechnicianLocationQuery(
            other.id, visit.public_id
        ),
    )
    assert not result.available
    assert result.reason == "not_found"
    assert result.work_order_id is None


def _rate(db, subscriber_id, public_id, *, rating, comment=None, representative=None):
    command = customer_work_order_selfcare.RateTechnicianCommand(
        context=CommandContext.system(
            actor=f"subscriber:{subscriber_id}",
            scope="customer:work_order:rating",
            reason="Customer feedback",
        ),
        subscriber_id=subscriber_id,
        work_order_public_id=public_id,
        rating=rating,
        comment=comment,
        representative=representative,
    )
    db_session_adapter.release_read_transaction(db)
    return customer_work_order_selfcare.rate_technician(db, command=command)


def test_vendor_label_is_not_a_technician_and_cannot_be_rated(db_session):
    from app.services.customer_experience_lifecycle import work_order_reference

    customer = _customer(db_session)
    visit = _visit(db_session, customer, status="completed")
    visit.assigned_to_name = "Vendor Organization"
    visit.technician_name = "Legacy arbitrary name"
    vendor = Vendor(name="Vendor Organization")
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=visit.id,
            status="assigned",
            assigned_vendor_id=vendor.id,
        )
    )
    db_session.commit()
    reference = work_order_reference(visit, db=db_session)
    assert reference.technician_name is None
    assert reference.technician_phone is None
    assert not any(action.key == "rate_technician" for action in reference.actions)
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError,
        match="native_technician_unavailable",
    ):
        _rate(db_session, customer.id, visit.public_id, rating=5)
    assert visit.technician_rating is None


def test_historical_feedback_replay_checks_ownership_before_returning_score(db_session):
    customer, stranger = _customer(db_session), _customer(db_session)
    visit = _visit(db_session, customer, status="completed")
    visit.metadata_ = {"technician_rating": {"rating": 4}}
    visit.assigned_to_name = "Vendor Organization"
    db_session.commit()
    assert _rate(db_session, customer.id, visit.public_id, rating=1).rating == 4
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError, match="work_order_not_found"
    ):
        _rate(db_session, stranger.id, visit.public_id, rating=1)


def test_projection_uses_bound_native_staff_and_active_eligibility(db_session):
    from app.services.customer_experience_lifecycle import work_order_reference

    customer = _customer(db_session)
    technician = _technician(db_session)
    user = db_session.get(SystemUser, technician.system_user_id)
    user.phone = "+234000000001"
    visit = _visit(db_session, customer, status="completed")
    visit.assigned_to_name = "Vendor display must not win"
    visit.technician_name = "Untrusted historical name"
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=visit.id,
            status="assigned",
            assigned_technician_id=technician.id,
            assignment_generation=uuid4(),
        )
    )
    db_session.commit()
    reference = work_order_reference(visit, db=db_session)
    assert reference.technician_name == "Field Engineer"
    assert reference.technician_phone == "+234000000001"
    assert any(
        action.key == "rate_technician" and action.allowed
        for action in reference.actions
    )
    technician.is_active = False
    db_session.commit()
    reference = work_order_reference(visit, db=db_session)
    assert reference.technician_name is None
    assert reference.technician_phone is None
    assert not any(action.key == "rate_technician" for action in reference.actions)
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError,
        match="native_technician_unavailable",
    ):
        _rate(db_session, customer.id, visit.public_id, rating=5)


def test_feedback_event_failure_rolls_back_rating_and_audit(db_session, monkeypatch):
    customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status="completed")
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=visit.id,
            status="assigned",
            assigned_technician_id=technician.id,
            assignment_generation=uuid4(),
        )
    )
    db_session.commit()
    job_id, customer_id, public_id = visit.id, customer.id, visit.public_id
    original_emit = customer_work_order_selfcare.emit_event

    def fail_after_staging(*args, **kwargs):
        original_emit(*args, **kwargs)
        raise RuntimeError("Synthetic event staging failure")

    monkeypatch.setattr(customer_work_order_selfcare, "emit_event", fail_after_staging)
    with pytest.raises(RuntimeError, match="Synthetic event staging failure"):
        _rate(db_session, customer_id, public_id, rating=5)
    db_session.expire_all()
    assert db_session.get(WorkOrder, job_id).technician_rating is None
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="work_order_technician_rated", entity_id=str(job_id))
        .count()
        == 0
    )
    from app.models.event_store import EventStore

    assert (
        db_session.query(EventStore).filter_by(subscriber_id=customer_id).count() == 0
    )


def test_missing_staff_binding_cannot_receive_new_rating_but_history_is_owned(
    db_session,
):
    from app.services.customer_experience_lifecycle import work_order_reference

    customer = _customer(db_session)
    technician = _technician(db_session)
    technician.system_user_id = None
    visit = _visit(db_session, customer, status="completed")
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=visit.id,
            status="assigned",
            assigned_technician_id=technician.id,
        )
    )
    db_session.commit()
    reference = work_order_reference(visit, db=db_session)
    assert reference.technician_name is None
    assert reference.technician_phone is None
    assert not any(action.key == "rate_technician" for action in reference.actions)
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError,
        match="native_technician_unavailable",
    ):
        _rate(db_session, customer.id, visit.public_id, rating=5)
    visit.metadata_ = {"technician_rating": {"rating": 4}}
    db_session.commit()
    assert _rate(db_session, customer.id, visit.public_id, rating=1).rating == 4
    stranger = _customer(db_session)
    db_session.commit()
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError, match="work_order_not_found"
    ):
        _rate(db_session, stranger.id, visit.public_id, rating=1)


@pytest.mark.parametrize("historical", [False, True])
def test_representative_transfer_denies_write_and_history_replay(
    db_session, historical
):
    customer = _customer(db_session)
    original, replacement = (
        Reseller(name="Original reseller"),
        Reseller(name="Replacement reseller"),
    )
    db_session.add_all([original, replacement])
    db_session.flush()
    actor = ResellerUser(reseller_id=original.id, is_active=True)
    current_actor = ResellerUser(reseller_id=replacement.id, is_active=True)
    db_session.add_all([actor, current_actor])
    customer.reseller_id = original.id
    visit = _visit(db_session, customer, status="completed")
    if historical:
        visit.metadata_ = {"technician_rating": {"rating": 4}}
    db_session.flush()
    scope = customer_work_order_selfcare.ResellerRatingScope(
        original.id,
        actor.id,
        customer_work_order_selfcare.RatingRepresentativeKind.reseller_user,
    )
    db_session.commit()
    # The adapter authorized the original reseller before this transfer.
    customer.reseller_id = replacement.id
    db_session.commit()
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError, match="work_order_not_found"
    ):
        _rate(db_session, customer.id, visit.public_id, rating=1, representative=scope)
    assert visit.technician_rating == (4 if historical else None)
    if historical:
        current_scope = customer_work_order_selfcare.ResellerRatingScope(
            replacement.id,
            current_actor.id,
            customer_work_order_selfcare.RatingRepresentativeKind.reseller_user,
        )
        assert (
            _rate(
                db_session,
                customer.id,
                visit.public_id,
                rating=1,
                representative=current_scope,
            ).rating
            == 4
        )
        current_actor.is_active = False
        db_session.commit()
        with pytest.raises(
            customer_work_order_selfcare.TechnicianRatingError,
            match="work_order_not_found",
        ):
            _rate(
                db_session,
                customer.id,
                visit.public_id,
                rating=1,
                representative=current_scope,
            )


def test_legacy_representative_uses_latest_active_canonical_binding(db_session):
    customer, login = _customer(db_session), _customer(db_session)
    original, replacement = (
        Reseller(name="Legacy old reseller"),
        Reseller(name="Legacy new reseller"),
    )
    db_session.add_all([original, replacement])
    db_session.flush()
    customer.reseller_id = original.id
    db_session.add_all(
        [
            ResellerUser(
                subscriber_id=login.id,
                reseller_id=original.id,
                is_active=True,
                created_at=datetime.now(UTC) - timedelta(days=1),
            ),
            ResellerUser(
                subscriber_id=login.id,
                reseller_id=replacement.id,
                is_active=True,
                created_at=datetime.now(UTC),
            ),
        ]
    )
    visit = _visit(db_session, customer, status="completed")
    visit.metadata_ = {"technician_rating": {"rating": 4}}
    db_session.commit()
    stale = customer_work_order_selfcare.ResellerRatingScope(
        original.id,
        login.id,
        customer_work_order_selfcare.RatingRepresentativeKind.subscriber,
    )
    with pytest.raises(
        customer_work_order_selfcare.TechnicianRatingError, match="work_order_not_found"
    ):
        _rate(db_session, customer.id, visit.public_id, rating=1, representative=stale)
    customer.reseller_id = replacement.id
    db_session.commit()
    current = customer_work_order_selfcare.ResellerRatingScope(
        replacement.id,
        login.id,
        customer_work_order_selfcare.RatingRepresentativeKind.subscriber,
    )
    assert (
        _rate(
            db_session, customer.id, visit.public_id, rating=1, representative=current
        ).rating
        == 4
    )
