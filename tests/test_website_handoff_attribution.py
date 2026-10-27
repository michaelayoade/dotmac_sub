"""Fast unit-lane enquiry/receipt tests; not deployed-schema acceptance."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.sales import Lead
from app.models.team_inbox import InboxMessage
from app.schemas.fiber_inquiry import FiberAttribution, FiberInquiryRequest
from app.schemas.lead_intake import AiLeadIntakeClassification
from app.schemas.website_handoff import WebsiteHandoffAttribution
from app.services import website_handoff_attribution
from app.services.sales import lead_intake
from tests.test_fiber_inquiry_webhook import _binding, _coverage_payload, _post
from tests.test_lead_intake import _context, _conversation


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
    snapshot = WebsiteHandoffAttribution(
        observation_id=uuid4(),
        integration_inbox_id=uuid4(),
        reference="FWA-" + "A" * 24,
        submitted_at=datetime.now(UTC),
        attribution=FiberAttribution(
            journey_id=uuid4(),
            utm_source="meta",
            campaign_id="test-campaign",
            ad_id="test-ad",
            landing_path="/coverage/",
        ),
    )

    def resolve(
        _db: Session, *, query: website_handoff_attribution.WebsiteHandoffMessageQuery
    ) -> WebsiteHandoffAttribution:
        assert query.message_id == message_id
        if resolver_fails:
            raise RuntimeError("simulated optional resolver failure")
        return snapshot

    monkeypatch.setattr(website_handoff_attribution, "resolve_for_message", resolve)
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
        assert lead.metadata_["website_enquiry_observation_id"] == str(
            snapshot.observation_id
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
