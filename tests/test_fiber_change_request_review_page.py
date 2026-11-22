"""Pending request review labels resolve from the stored identities."""

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
)
from app.models.subscriber import Subscriber
from app.models.vendor_routes import Vendor
from app.services.web_network_fiber_plant import change_requests_page_data


def test_review_page_resolves_vendor_and_requester_on_current_page(db_session):
    vendor = Vendor(name="Field Partner")
    requester = Subscriber(
        first_name="Ada", last_name="Okafor", email="ada@example.test"
    )
    db_session.add_all((vendor, requester))
    db_session.flush()
    request = FiberChangeRequest(
        asset_type="splitter",
        operation=FiberChangeRequestOperation.create,
        payload={},
        requested_by_vendor_id=vendor.id,
        requested_by_person_id=requester.id,
    )
    db_session.add(request)
    db_session.flush()

    page = change_requests_page_data(db_session, bulk_status=None, skipped=None)

    assert page["vendor_names"][str(vendor.id)] == "Field Partner"
    assert page["requester_names"][str(requester.id)] == "Ada Okafor"
