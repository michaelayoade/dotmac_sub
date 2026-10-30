"""Fast unit-lane receipt browse fixtures, not deployed-schema acceptance."""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInbox
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.schemas.lead_acquisition import WebsiteEnquiryStatus
from app.schemas.website_enquiry import (
    EnquiryLeadFilter,
    EnquiryPageSize,
    WebsiteEnquiryDetailQuery,
    WebsiteEnquiryListQuery,
)
from app.services import website_enquiry_queries
from tests.test_fiber_inquiry_webhook import _binding, _coverage_payload, _post
from tests.test_website_handoff_attribution import _manually_authored_handoff


def test_resolved_receipt_remains_searchable_without_a_lead(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(db_session, monkeypatch)
    payload = _coverage_payload(coordinates=False)
    payload.update(
        form_version="fiber-whatsapp-v1",
        whatsapp_account_id="123456",
        full_name="100% Abuja Prospect",
    )
    outcome = _post(db_session, binding.id, payload, "browse-retained-receipt")
    # Represent retained historical state as query fixture data; this does not
    # test or assert acceptance of the status-transition command.
    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation is not None
    conversation.status = InboxConversationStatus.resolved.value
    db_session.commit()
    page = website_enquiry_queries.list_website_enquiries(
        db_session,
        query=WebsiteEnquiryListQuery(
            search="100%", lead_filter=EnquiryLeadFilter.unlinked
        ),
    )
    assert page.total == 1 and len(page.items) == 1
    row = page.items[0]
    assert row.reference == outcome.reference
    assert row.linked_lead_id is None
    assert row.enquiry is not None and row.enquiry.full_name == "100% Abuja Prospect"
    assert (
        website_enquiry_queries.list_website_enquiries(
            db_session, query=WebsiteEnquiryListQuery(search="missing%")
        ).total
        == 0
    )
    detail = website_enquiry_queries.get_website_enquiry(
        db_session, query=WebsiteEnquiryDetailQuery(receipt_id=row.receipt_id)
    )
    assert detail == row
    assert not db_session.dirty and not db_session.new


def test_filtered_count_pagination_and_exact_lead_link_share_receipt_scope(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead_id, _origin_id = _manually_authored_handoff(db_session, monkeypatch)
    page = website_enquiry_queries.list_website_enquiries(
        db_session,
        query=WebsiteEnquiryListQuery(
            source="GOOGLE",
            campaign="abuja_home",
            lead_filter=EnquiryLeadFilter.linked,
            page=999,
            per_page=EnquiryPageSize.ten,
        ),
    )
    assert page.total == 1 and page.total_pages == 1 and page.query.page == 1
    assert len(page.items) == 1 and page.items[0].linked_lead_id == lead_id
    assert page.items[0].details_status is WebsiteEnquiryStatus.available
    day = page.items[0].received_at.date()
    assert (
        website_enquiry_queries.list_website_enquiries(
            db_session,
            query=WebsiteEnquiryListQuery(received_from=day, received_to=day),
        ).total
        == 1
    )
    assert (
        website_enquiry_queries.list_website_enquiries(
            db_session,
            query=WebsiteEnquiryListQuery(lead_filter=EnquiryLeadFilter.unlinked),
        ).total
        == 0
    )
    assert (
        website_enquiry_queries.list_website_enquiries(
            db_session, query=WebsiteEnquiryListQuery(campaign="different-campaign")
        ).total
        == 0
    )
    assert (
        website_enquiry_queries.get_website_enquiry(
            db_session, query=WebsiteEnquiryDetailQuery(receipt_id=uuid4())
        )
        is None
    )


def test_invalid_retained_payload_does_not_silently_disappear_from_totals(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(db_session, monkeypatch)
    payload = _coverage_payload(coordinates=False)
    payload.update(form_version="fiber-whatsapp-v1", whatsapp_account_id="123456")
    _post(db_session, binding.id, payload, "browse-invalid-fixture")
    receipt = db_session.query(IntegrationInbox).one()
    # Read-only recovery fixture: emulate malformed retained data, never a new
    # accepted webhook or an approved update of signed production evidence.
    receipt.payload_json = {"form_version": "fiber-whatsapp-v1"}
    db_session.commit()
    page = website_enquiry_queries.list_website_enquiries(
        db_session, query=WebsiteEnquiryListQuery()
    )
    assert page.total == 1 and len(page.items) == 1
    assert page.items[0].details_status is WebsiteEnquiryStatus.invalid
    assert page.items[0].enquiry is None
