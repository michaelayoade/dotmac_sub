"""Fast reviewed identity projection tests for the selected native member."""

from uuid import uuid4

import pytest

from app.models.dispatch import WorkOrderAssignmentQueue
from app.models.field_identity import FieldIdentityRevision
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.vendor_routes import Vendor
from app.services import customer_field_identity as owner
from tests.test_customer_field_identity import assigned as reviewed_identity_fixture

assigned = reviewed_identity_fixture


@pytest.fixture
def vendor_assigned(db_session, assigned):
    query, _, subject, _, queue_id, _, identity_id = assigned
    native = Vendor(name="Selected native contractor", is_active=True)
    db_session.add(native)
    db_session.flush()
    bridge = FieldVendor(name=native.name, native_vendor_id=native.id, is_active=True)
    db_session.add(bridge)
    db_session.flush()
    member = FieldVendorUser(
        vendor_id=bridge.id, system_user_id=subject, is_active=True
    )
    db_session.add(member)
    db_session.flush()
    queue = db_session.get(WorkOrderAssignmentQueue, queue_id)
    queue.assigned_technician_id = None
    queue.assigned_vendor_id = native.id
    queue.assigned_vendor_user_id = member.id
    member_id = member.id
    db_session.commit()
    return query, member_id, queue_id, identity_id


def test_exact_selected_reviewed_vendor_person_can_be_disclosed(
    db_session, vendor_assigned
):
    query, *_ = vendor_assigned
    result = owner.card(db_session, query=query)
    assert result.card and result.card.display_name == "Reviewed Engineer"
    assert result.card.company_name == "Reviewed Company"


@pytest.mark.parametrize(
    "fault",
    ["missing_selection", "inactive_member", "unreviewed", "generation_missing"],
)
def test_vendor_identity_fails_closed_without_current_exact_review(
    db_session, vendor_assigned, fault
):
    query, member_id, queue_id, identity_id = vendor_assigned
    if fault == "missing_selection":
        db_session.get(
            WorkOrderAssignmentQueue, queue_id
        ).assigned_vendor_user_id = None
    elif fault == "inactive_member":
        db_session.get(FieldVendorUser, member_id).is_active = False
    elif fault == "unreviewed":
        identity = db_session.get(FieldIdentityRevision, identity_id)
        identity.state = "pending"
        identity.reviewer_id = None
        identity.reviewed_at = None
        identity.review_reason = None
    else:
        db_session.get(WorkOrderAssignmentQueue, queue_id).assignment_generation = None
    db_session.commit()
    assert owner.card(db_session, query=query).card is None


def test_superseded_vendor_generation_cannot_stream_portrait(
    db_session, vendor_assigned
):
    query, *_ = vendor_assigned
    with pytest.raises(owner.CustomerFieldIdentityError):
        owner.portrait(
            db_session,
            query=owner.CustomerFieldPortraitQuery(
                query.subscriber_id, query.work_order_id, uuid4(), 1
            ),
        )
