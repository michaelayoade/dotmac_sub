"""Fast unit-lane enquiry/receipt tests; not deployed-schema acceptance."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInbox
from app.models.sales import Lead
from app.models.team_inbox import InboxMessage
from app.schemas.fiber_inquiry import FiberInquiryRequest
from app.schemas.lead_acquisition import LeadAcquisitionQuery, WebsiteEnquiryStatus
from app.schemas.lead_intake import AiLeadIntakeClassification
from app.schemas.sales import LeadOriginCaptureCreate
from app.schemas.website_handoff import (
    InboxAcquisitionContext,
    WebsiteHandoffAttribution,
)
from app.services import web_sales, website_handoff_attribution
from app.services.sales import lead_intake, lifecycle
from tests.test_fiber_inquiry_webhook import _binding, _coverage_payload, _post
from tests.test_lead_intake import _context, _conversation
from tests.test_lead_origin_values import website
from tests.test_web_sales_lead_authoring import _author, _staff_owner


def test_enquiry_only_receipt_replays_without_creating_a_lead(
    db_session: Session, monkeypatch
) -> None:
    binding = _binding(db_session, monkeypatch)
    wire = _coverage_payload(coordinates=False)
    wire.update(form_version="fiber-whatsapp-v1", whatsapp_account_id="123456")
    command = FiberInquiryRequest.model_validate_json(json.dumps(wire))
    receipt = _post(
        db_session,
        binding.id,
        command.model_dump(mode="json", exclude_none=True),
        "handoff-unit-1",
    )
    replay = _post(
        db_session,
        binding.id,
        command.model_dump(mode="json", exclude_none=True),
        "handoff-unit-1",
    )
    assert receipt.reference is not None and receipt.reference.startswith("FWA-")
    assert replay.reference == receipt.reference
    assert replay.replayed
    assert db_session.query(Lead).count() == 0
    message = db_session.get(InboxMessage, receipt.message_id)
    assert message is not None and "Installation address:" in message.body


@pytest.mark.parametrize("resolver_fails", [False, True])
def test_optional_attribution_preserves_classified_lead_and_replay(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, resolver_fails: bool
) -> None:
    conversation, message = _conversation(db_session)
    conversation_id, message_id = conversation.id, message.id
    binding = _binding(db_session, monkeypatch)
    wire = _coverage_payload(coordinates=False)
    wire.update(form_version="fiber-whatsapp-v1", whatsapp_account_id="123456")
    wire["attribution"].update(
        utm_source="meta", campaign_id="test-campaign", ad_id="test-ad"
    )
    confirmation = _post(db_session, binding.id, wire, "classified-attribution-receipt")
    receipt = db_session.query(IntegrationInbox).one()
    request = FiberInquiryRequest.model_validate_json(json.dumps(wire))
    assert request.attribution is not None and confirmation.reference is not None
    snapshot = WebsiteHandoffAttribution(
        observation_id=uuid4(),
        integration_inbox_id=receipt.id,
        reference=confirmation.reference,
        submitted_at=request.submitted_at,
        attribution=request.attribution,
    )

    def resolve(
        _db: Session,
        *,
        query: website_handoff_attribution.WebsiteHandoffConversationQuery,
    ) -> WebsiteHandoffAttribution:
        assert query.message_id == message_id
        if resolver_fails:
            raise RuntimeError("simulated optional resolver failure")
        return snapshot

    monkeypatch.setattr(
        website_handoff_attribution, "resolve_for_conversation", resolve
    )
    db_session.commit()
    classification = AiLeadIntakeClassification(
        intent="new_connection",
        intent_confidence=0.96,
        party_type="individual",
        party_type_confidence=0.96,
        clarification_question=None,
    )
    outcome = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"website-candidate:{message_id}"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=classification,
            allow_invitation=False,
        ),
    )
    lead = db_session.get(Lead, outcome.lead_id)
    assert outcome.action == "lead_created" and lead is not None
    assert lead.origin_capture is not None
    if resolver_fails:
        assert lead.metadata_["website_attribution_failure"] == "RuntimeError"
        assert lead.origin_capture.external_form_id is None
    else:
        assert lead.origin_capture.external_form_id == "fiber-whatsapp-v1"
        assert lead.origin_capture.external_ad_id == "test-ad"
        assert lead.origin_capture.journey_id == snapshot.attribution.journey_id
        assert lead.origin_capture.integration_inbox_id == receipt.id
        assert lead.origin_capture.customer_reference == snapshot.reference
        assert lead.metadata_["website_enquiry_observation_id"] == str(
            snapshot.observation_id
        )
        view = lifecycle.get_lead_acquisition_detail(
            db_session, query=LeadAcquisitionQuery(lead_id=lead.id)
        )
        assert view.enquiry_status is WebsiteEnquiryStatus.available
        assert (
            view.enquiry is not None and view.enquiry.full_name == "Coverage Prospect"
        )
    lead_id = lead.id
    db_session.commit()
    replay = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"website-candidate-replay:{message_id}"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=classification,
            allow_invitation=False,
        ),
    )
    assert replay.lead_id == lead_id and replay.replayed
    assert db_session.query(Lead).count() == 1


def _manually_authored_handoff(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID]:
    actor = _staff_owner(db_session)
    binding = _binding(db_session, monkeypatch)
    wire = _coverage_payload(coordinates=False)
    wire.update(form_version="fiber-whatsapp-v1", whatsapp_account_id="123456")
    confirmation = _post(db_session, binding.id, wire, "manual-attribution-receipt")
    receipt = db_session.query(IntegrationInbox).one()
    request = FiberInquiryRequest.model_validate_json(json.dumps(wire))
    assert request.attribution is not None and confirmation.reference is not None
    snapshot = website().model_copy(
        update={
            "integration_inbox_id": receipt.id,
            "reference": confirmation.reference,
            "attribution": request.attribution,
            "submitted_at": request.submitted_at,
        }
    )
    conversation, _message = _conversation(db_session)
    conversation_id = conversation.id
    db_session.commit()

    def resolve(
        _db: Session,
        *,
        query: website_handoff_attribution.WebsiteHandoffConversationQuery,
    ) -> WebsiteHandoffAttribution:
        assert query.conversation_id == conversation_id
        return snapshot

    monkeypatch.setattr(
        website_handoff_attribution, "resolve_for_conversation", resolve
    )
    outcome = _author(db_session, actor, inbox_conversation_id=str(conversation_id))
    lead = db_session.get(Lead, outcome.lead_id)
    assert lead is not None and lead.origin_capture is not None
    assert lead.lead_source == "Whatsapp"
    assert lead.origin_capture.capture_method == "inbox_form"
    assert lead.origin_capture.capture_source != "ai.intake"
    assert lead.origin_capture.integration_inbox_id == receipt.id
    assert lead.origin_capture.customer_reference == snapshot.reference
    assert db_session.query(Lead).count() == 1
    lead_id, origin_id = lead.id, lead.origin_capture.id
    db_session.rollback()
    return lead_id, origin_id


def test_manual_creation_captures_the_same_receipt_without_ai(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manually_authored_handoff(db_session, monkeypatch)


def test_saved_enquiry_details_reach_the_lead_page_without_changing_contact(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead_id, origin_id = _manually_authored_handoff(db_session, monkeypatch)
    lead = db_session.get(Lead, lead_id)
    assert lead is not None
    before = (lead.party_id, lead.address, lead.lead_source)
    view = lifecycle.get_lead_acquisition_detail(
        db_session, query=LeadAcquisitionQuery(lead_id=lead_id)
    )
    assert view.enquiry_status is WebsiteEnquiryStatus.available
    assert view.origin is not None and view.origin.origin_id == origin_id
    assert view.enquiry is not None
    assert view.enquiry.full_name == "Coverage Prospect"
    assert view.enquiry.phone == "+2348031234567"
    assert view.enquiry.address == "12 Example Close, Wuse 2, Abuja"
    assert view.origin.tags.campaign_id == "campaign-123"
    context = web_sales.build_lead_detail_context(db_session, lead_id=str(lead_id))
    assert context["acquisition"] == view
    assert context["contact"] is not None
    assert context["contact"].name != view.enquiry.full_name
    assert (lead.party_id, lead.address, lead.lead_source) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_a_claimed_receipt_cannot_be_attached_to_a_second_origin(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead_id, _origin_id = _manually_authored_handoff(db_session, monkeypatch)
    lead = db_session.get(Lead, lead_id)
    assert lead is not None and lead.origin_capture is not None
    receipt_id = lead.origin_capture.integration_inbox_id
    snapshot = website().model_copy(update={"integration_inbox_id": receipt_id})
    monkeypatch.setattr(
        website_handoff_attribution,
        "resolve_for_conversation",
        lambda _db, *, query: snapshot,
    )
    result = lifecycle._prepare_inbox_acquisition(
        db_session,
        context=InboxAcquisitionContext(conversation_id=uuid4()),
        original=LeadOriginCaptureCreate(
            capture_method="inbox_form",
            source_platform="team_inbox",
            capture_source="pytest",
            capture_reason="second claim regression",
        ),
    )
    assert result is None
    assert lead.origin_capture.integration_inbox_id == receipt_id
