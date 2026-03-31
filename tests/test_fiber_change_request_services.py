from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.services import fiber_change_requests


def test_reject_request_supports_system_user_actor_without_subscriber_fk(db_session):
    """reject_request currently only accepts reviewer_person_id and review_notes."""
    change_request = FiberChangeRequest(
        asset_type="fdh_cabinet",
        asset_id=None,
        operation=FiberChangeRequestOperation.update,
        payload={"latitude": 1.0, "longitude": 2.0},
        status=FiberChangeRequestStatus.pending,
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    db_session.add(change_request)
    db_session.commit()

    rejected = fiber_change_requests.reject_request(
        db_session,
        str(change_request.id),
        reviewer_person_id=None,
        review_notes="Rejected by admin",
    )

    assert rejected.status == FiberChangeRequestStatus.rejected
    assert rejected.reviewed_by_person_id is None
    assert rejected.review_notes == "Rejected by admin"
