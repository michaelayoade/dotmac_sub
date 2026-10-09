"""Focused behavior contracts for Inbox Lead intake."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakeConfig
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.event_store import EventStore
from app.models.lead_intake import LeadIntakeInvitation, LeadIntakePartyType
from app.models.party import Party, PartyContactPoint, PartyRole
from app.models.sales import Lead, LeadOriginCapture
from app.models.service_team import ServiceTeam
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationLeadLink,
    InboxConversationParticipant,
    InboxMessage,
)
from app.schemas.ai_intake import (
    AiIntakeCategory,
    AiIntakeClassification,
    AiIntakeIntent,
    AiIntakeOutcome,
    AiIntakePartyType,
    AiIntakeReason,
    AiIntakeStatus,
)
from app.schemas.lead_intake import (
    AiLeadIntakeClassification,
    LeadCandidateAttribution,
    LeadIntakeSubmission,
    LeadIntakeTemplateDraft,
    ResolvedLeadIntakeAddress,
)
from app.services import (
    ai_conversation_intake,
    ai_intake,
    lead_intake_ai,
    team_inbox_customer_completion,
)
from app.services.domain_errors import DomainError
from app.services.events import dispatcher as event_dispatcher
from app.services.events.handlers import lead_intake as lead_intake_event_handler
from app.services.events.types import Event, EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext
from app.services.sales import lead_intake
from app.services.settings_cache import SettingsCache


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:lead-intake",
        scope="sales.lead_intake:test",
        reason="focused Lead intake behavior test",
        idempotency_key=key,
    )


@pytest.mark.parametrize(
    "status", [AiIntakeStatus.awaiting_follow_up, AiIntakeStatus.fallback]
)
def test_sales_capture_precedes_clarification_and_survives_later_complaint(
    db_session, monkeypatch: pytest.MonkeyPatch, status: AiIntakeStatus
):
    def hold_dispatch(_db: Session, _callback: Callable[[Session], None]) -> None:
        # Model a worker that has not consumed the committed outbox event yet.
        # Keep real event persistence so the pending gate and repair scan are tested.
        return None

    monkeypatch.setattr(event_dispatcher, "run_after_commit", hold_dispatch)
    conversation, message = _instagram_conversation(db_session)
    metadata: dict[str, object] = {
        **(message.metadata_ or {}),
        "ai_intake_status": status.value,
        "ai_intake_requires_follow_up": status is AiIntakeStatus.awaiting_follow_up,
        "ai_intent": "coverage_request",
        "ai_confidence": 0.96,
        "ai_party_type": "individual",
        "ai_party_type_confidence": 0.96,
    }
    classification = AiIntakeClassification(
        intent=AiIntakeIntent.coverage_request,
        category=AiIntakeCategory.coverage_request,
        confidence=0.96,
        party_type=AiIntakePartyType.individual,
        party_type_confidence=0.96,
        requires_follow_up=status is AiIntakeStatus.awaiting_follow_up,
    )
    outcome = AiIntakeOutcome(
        status=status,
        reason=AiIntakeReason.low_confidence,
        classification=classification,
    )
    for _ in range(2):
        ai_conversation_intake._stage_lead_candidate_classified(
            db_session,
            inbound=message,
            conversation=conversation,
            outcome=outcome,
            metadata=metadata,
        )
        db_session.flush()
    message.metadata_ = metadata
    db_session.commit()
    events = db_session.scalars(
        select(EventStore).where(
            EventStore.event_type == EventType.ai_intake_lead_candidate_classified.value
        )
    ).all()
    assert len(events) == 1
    assert metadata["ai_lead_candidate_event_id"] == str(events[0].event_id)
    assert team_inbox_customer_completion._classified_sales_candidate_pending(
        db_session, conversation
    )
    assert any(
        finding.conversation_id == conversation.id
        for finding in lead_intake.classified_candidate_drift(
            db_session,
            query=lead_intake.ClassifiedCandidateDriftQuery(
                since=datetime.now(UTC) - timedelta(days=1)
            ),
        )
    )
    # Later routing/classification cannot retract the earlier message's event.
    ai_conversation_intake._stage_lead_candidate_classified(
        db_session,
        inbound=message,
        conversation=conversation,
        outcome=AiIntakeOutcome(
            status=AiIntakeStatus.classified,
            reason=AiIntakeReason.classified,
            classification=AiIntakeClassification(
                intent=AiIntakeIntent.complaint,
                category=AiIntakeCategory.complaint,
                confidence=0.96,
                requires_follow_up=False,
            ),
        ),
        metadata={},
    )
    result = lead_intake_ai.apply_inbox_intake_handoff(
        db_session,
        conversation_id=conversation.id,
        message_id=message.id,
        allow_invitation=False,
    )
    assert result is not None and result.lead_id is not None
    assert not team_inbox_customer_completion._classified_sales_candidate_pending(
        db_session, conversation
    )


@pytest.mark.parametrize(
    "intent,confidence,party_type,party_confidence,status",
    [
        (
            AiIntakeIntent.complaint,
            0.96,
            AiIntakePartyType.individual,
            0.96,
            AiIntakeStatus.classified,
        ),
        (
            AiIntakeIntent.general_enquiry,
            0.96,
            AiIntakePartyType.individual,
            0.96,
            AiIntakeStatus.classified,
        ),
        (
            AiIntakeIntent.coverage_request,
            0.4,
            AiIntakePartyType.individual,
            0.96,
            AiIntakeStatus.fallback,
        ),
        (
            AiIntakeIntent.coverage_request,
            0.96,
            AiIntakePartyType.unknown,
            0.0,
            AiIntakeStatus.fallback,
        ),
        (
            AiIntakeIntent.coverage_request,
            0.96,
            AiIntakePartyType.individual,
            0.4,
            AiIntakeStatus.awaiting_follow_up,
        ),
        (
            AiIntakeIntent.coverage_request,
            0.96,
            AiIntakePartyType.individual,
            0.96,
            AiIntakeStatus.classification_unavailable,
        ),
    ],
)
def test_sales_capture_refuses_complaints_and_unreliable_classification(
    db_session,
    intent: AiIntakeIntent,
    confidence: float,
    party_type: AiIntakePartyType,
    party_confidence: float,
    status: AiIntakeStatus,
):
    conversation, message = _conversation(db_session)
    metadata: dict[str, object] = {}
    ai_conversation_intake._stage_lead_candidate_classified(
        db_session,
        inbound=message,
        conversation=conversation,
        outcome=AiIntakeOutcome(
            status=status,
            reason=AiIntakeReason.low_confidence,
            classification=AiIntakeClassification(
                intent=intent,
                category=AiIntakeCategory.general_enquiry,
                confidence=confidence,
                party_type=party_type,
                party_type_confidence=party_confidence,
                requires_follow_up=False,
            ),
        ),
        metadata=metadata,
    )
    assert "ai_lead_candidate_event_id" not in metadata


def _staff_and_team(db_session) -> tuple[SystemUser, ServiceTeam]:
    staff = SystemUser(
        first_name="Sales",
        last_name="Agent",
        email=f"lead-intake-{uuid4().hex}@example.com",
        is_active=True,
    )
    team = ServiceTeam(name=f"Sales Intake {uuid4().hex[:8]}", is_active=True)
    db_session.add_all([staff, team])
    db_session.commit()
    return staff, team


def test_confident_sales_with_unknown_customer_type_is_durable_staff_review(
    db_session, monkeypatch: pytest.MonkeyPatch
):
    conversation, message = _instagram_conversation(db_session)
    outcome = AiIntakeOutcome(
        status=AiIntakeStatus.awaiting_follow_up,
        reason=AiIntakeReason.low_confidence,
        classification=AiIntakeClassification(
            intent=AiIntakeIntent.coverage_request,
            category=AiIntakeCategory.coverage_request,
            confidence=0.96,
            party_type=AiIntakePartyType.unknown,
            party_type_confidence=0.0,
            requires_follow_up=True,
        ),
    )
    metadata = {**(message.metadata_ or {}), **ai_intake.route_metadata(outcome)}
    ai_conversation_intake._stage_lead_candidate_classified(
        db_session,
        inbound=message,
        conversation=conversation,
        outcome=outcome,
        metadata=metadata,
    )
    message.metadata_ = metadata
    db_session.commit()
    assert metadata["ai_sales_candidate_review_reason"] == "customer_type_required"
    assert "ai_lead_candidate_event_id" not in metadata
    assert db_session.scalar(select(func.count(Lead.id))) == 0
    assert team_inbox_customer_completion._classified_sales_candidate_pending(
        db_session, conversation
    )
    findings = lead_intake.classified_candidate_drift(
        db_session,
        query=lead_intake.ClassifiedCandidateDriftQuery(
            since=datetime.now(UTC) - timedelta(days=1)
        ),
    )
    finding = next(item for item in findings if item.conversation_id == conversation.id)
    assert finding.review_reason is not None
    assert finding.review_reason.value == "customer_type_required"
    assert finding.classification.party_type.value == "unknown"


def _conversation(db_session) -> tuple[InboxConversation, InboxMessage]:
    endpoint = f"23480{uuid4().int % 10**8:08d}"
    conversation = InboxConversation(
        channel_type="whatsapp",
        contact_address=endpoint,
        external_thread_id=f"wa-{uuid4().hex}",
        metadata_={"contact_resolution": {"status": "unmatched"}},
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="whatsapp",
        direction="inbound",
        body="I need a new internet connection in Abuja",
        from_address=endpoint,
        external_message_id=f"wamid.{uuid4().hex}",
        metadata_={"provider": "whatsapp", "phone_number_id": "phone-1"},
    )
    db_session.add(message)
    db_session.flush()
    db_session.add(
        InboxConversationParticipant(
            conversation_id=conversation.id,
            channel_type="whatsapp",
            normalized_endpoint=endpoint,
            provider_account_scope="phone-1",
            admission_source="inbound_from",
            admission_message_id=message.id,
        )
    )
    db_session.commit()
    return conversation, message


def _instagram_conversation(db_session) -> tuple[InboxConversation, InboxMessage]:
    endpoint = str(17841400000000000 + uuid4().int % 10**10)
    account_id = f"ig-{uuid4().hex}"
    conversation = InboxConversation(
        channel_type="instagram_dm",
        contact_address=endpoint,
        external_thread_id=f"instagram_dm:{endpoint}",
        subject="giftzara_lifestyle",
        metadata_={
            "contact_name": "giftzara_lifestyle",
            "contact_resolution": {"status": "unmatched"},
        },
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="instagram_dm",
        direction="inbound",
        body="I need a new internet connection for my business",
        from_address=endpoint,
        external_message_id=f"m_ig_{uuid4().hex}",
        metadata_={
            "provider": "meta_social",
            "provider_account_scope": account_id,
        },
    )
    db_session.add(message)
    db_session.flush()
    db_session.add(
        InboxConversationParticipant(
            conversation_id=conversation.id,
            channel_type="instagram_dm",
            normalized_endpoint=endpoint,
            provider_account_scope=account_id,
            admission_source="inbound_from",
            admission_message_id=message.id,
        )
    )
    db_session.commit()
    return conversation, message


def _published_template(
    db_session,
    *,
    staff: SystemUser,
    team: ServiceTeam,
    party_type: LeadIntakePartyType = LeadIntakePartyType.individual,
):
    staff_id = staff.id
    team_id = team.id
    db_session.commit()
    template_id = uuid4()
    draft = LeadIntakeTemplateDraft(
        party_type=party_type.value,
        name=f"{party_type.value.title()} intake",
        heading="Tell us about your connection request",
        introduction="We need a few details to assess your request.",
        privacy_notice="We use these details only to process this enquiry.",
        invitation_message="Please complete this secure form: {link}",
        confirmation_message="Your details have been saved for our Sales team.",
        thank_you_message="Thank you. Our Sales team will contact you.",
        target_service_team_id=team_id,
    )
    lead_intake.mutate_template(
        db_session,
        lead_intake.TemplateCommand(
            context=_context(f"template-create:{template_id}"),
            action=lead_intake.TemplateAction.create,
            actor_system_user_id=staff_id,
            template_id=template_id,
            draft=draft,
        ),
    )
    outcome = lead_intake.mutate_template(
        db_session,
        lead_intake.TemplateCommand(
            context=_context(f"template-publish:{template_id}"),
            action=lead_intake.TemplateAction.publish,
            actor_system_user_id=staff_id,
            template_id=template_id,
        ),
    )
    return db_session.get(lead_intake.LeadIntakeTemplate, outcome.template_id)


def test_ai_classification_accepts_closed_json_vocabulary():
    item = AiLeadIntakeClassification.model_validate(
        {
            "intent": "coverage_request",
            "intent_confidence": 0.93,
            "party_type": "organization",
            "party_type_confidence": 0.88,
            "clarification_question": None,
        }
    )
    assert item.intent.value == "coverage_request"
    with pytest.raises(ValueError):
        AiLeadIntakeClassification.model_validate(
            {
                "intent": "upgrade",
                "intent_confidence": 0.93,
                "party_type": "organization",
                "party_type_confidence": 0.88,
                "clarification_question": None,
            }
        )


def test_published_template_is_immutable(db_session):
    staff, team = _staff_and_team(db_session)
    template = _published_template(db_session, staff=staff, team=team)
    template_id = template.id
    staff_id = staff.id
    team_id = team.id
    db_session.commit()
    with pytest.raises(lead_intake.LeadIntakeError) as exc:
        lead_intake.mutate_template(
            db_session,
            lead_intake.TemplateCommand(
                context=_context(f"template-update:{template_id}"),
                action=lead_intake.TemplateAction.update,
                actor_system_user_id=staff_id,
                template_id=template_id,
                draft=LeadIntakeTemplateDraft(
                    party_type="individual",
                    name="Changed",
                    heading="Changed",
                    privacy_notice="Privacy",
                    invitation_message="Complete {link}",
                    confirmation_message="Saved",
                    thank_you_message="Thanks",
                    target_service_team_id=team_id,
                ),
            ),
        )
    assert exc.value.code == "sales.lead_intake.published_template_immutable"


def test_high_confidence_unknown_meta_prospect_receives_one_auto_invitation(
    db_session,
):
    staff, team = _staff_and_team(db_session)
    _published_template(db_session, staff=staff, team=team)
    _published_template(
        db_session,
        staff=staff,
        team=team,
        party_type=LeadIntakePartyType.organization,
    )
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.integration,
                key="lead_intake_auto_send_enabled",
                value_type=SettingValueType.boolean,
                value_text="true",
                is_active=True,
            ),
            AiIntakeConfig(
                scope_key=f"lead-intake-{uuid4().hex}",
                channel_type="whatsapp",
                is_enabled=True,
                confidence_threshold=0.8,
                allow_followup_questions=True,
                max_clarification_turns=1,
            ),
        ]
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.integration.value, "lead_intake_auto_send_enabled"
    )
    conversation, message = _conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    db_session.commit()

    outcome = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"auto-assess:{message_id}"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=AiLeadIntakeClassification(
                intent="new_connection",
                intent_confidence=0.96,
                party_type="individual",
                party_type_confidence=0.94,
                clarification_question=None,
            ),
            provider_label="pytest",
            model_label="classifier",
        ),
    )

    assert outcome.action == "invite_issued"
    assert outcome.token
    assert outcome.invitation_id
    invitations = db_session.scalars(
        select(LeadIntakeInvitation).where(
            LeadIntakeInvitation.conversation_id == conversation_id,
            LeadIntakeInvitation.auto_issued.is_(True),
        )
    ).all()
    assert len(invitations) == 1
    assert outcome.lead_id is not None


def test_final_instagram_sales_classification_creates_lead_without_form(
    db_session,
):
    conversation, message = _instagram_conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    db_session.commit()

    outcome = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"classified-candidate:{message_id}"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=AiLeadIntakeClassification(
                intent="new_connection",
                intent_confidence=0.96,
                party_type="organization",
                party_type_confidence=0.94,
                clarification_question=None,
            ),
            provider_label="pytest",
            model_label="classifier",
            attribution=LeadCandidateAttribution(
                campaign_ref="ig-ref-1",
                external_ad_id="ig-ad-1",
                referral_source="ADS",
                referral_type="OPEN_THREAD",
            ),
        ),
    )

    link = db_session.scalar(
        select(InboxConversationLeadLink).where(
            InboxConversationLeadLink.conversation_id == conversation.id,
            InboxConversationLeadLink.is_active.is_(True),
        )
    )
    lead = db_session.get(Lead, outcome.lead_id)
    origin = db_session.scalar(
        select(LeadOriginCapture).where(LeadOriginCapture.lead_id == outcome.lead_id)
    )

    assert outcome.action == "lead_created"
    assert lead is not None and lead.party_id == outcome.party_id
    assert lead.metadata_["profile_completeness"] == "provisional"
    assert lead.metadata_["meta_referral_source"] == "ADS"
    assert link is not None and link.lead_id == lead.id
    assert link.link_source == "ai_lead_candidate"
    assert origin is not None
    assert origin.capture_method == "inbox_classification"
    assert origin.external_ad_id == "ig-ad-1"
    assert db_session.scalar(select(func.count(LeadIntakeInvitation.id))) == 0

    db_session.commit()
    replay = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"classified-candidate-replay:{message_id}"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=AiLeadIntakeClassification(
                intent="new_connection",
                intent_confidence=0.96,
                party_type="organization",
                party_type_confidence=0.94,
                clarification_question=None,
            ),
        ),
    )
    assert replay.action == "lead_exists"
    assert replay.replayed is True
    assert replay.lead_id == outcome.lead_id
    assert db_session.scalar(select(func.count(Lead.id))) == 1


def test_auto_form_enriches_existing_classified_lead_without_duplicate(db_session):
    staff, team = _staff_and_team(db_session)
    _published_template(db_session, staff=staff, team=team)
    _published_template(
        db_session,
        staff=staff,
        team=team,
        party_type=LeadIntakePartyType.organization,
    )
    db_session.commit()
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.integration,
                key="lead_intake_auto_send_enabled",
                value_type=SettingValueType.boolean,
                value_text="true",
                is_active=True,
            ),
            AiIntakeConfig(
                scope_key=f"lead-intake-{uuid4().hex}",
                channel_type="instagram_dm",
                is_enabled=True,
                confidence_threshold=0.8,
                allow_followup_questions=True,
                max_clarification_turns=1,
            ),
        ]
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.integration.value, "lead_intake_auto_send_enabled"
    )
    conversation, message = _instagram_conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    db_session.commit()
    assessed = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"classified-form:{message_id}"),
            conversation_id=conversation_id,
            message_id=message_id,
            classification=AiLeadIntakeClassification(
                intent="coverage_request",
                intent_confidence=0.97,
                party_type="organization",
                party_type_confidence=0.96,
                clarification_question=None,
            ),
        ),
    )
    assert assessed.token and assessed.lead_id
    db_session.commit()

    submitted = lead_intake.submit_form(
        db_session,
        lead_intake.SubmitLeadIntakeCommand(
            context=_context(f"classified-submit:{assessed.invitation_id}"),
            token=assessed.token,
            submission=LeadIntakeSubmission(
                organization_name="Elite Iqraa Academy",
                representative_name="Amina Bello",
                representative_role="Administrator",
                latitude=9.0765,
                longitude=7.3986,
                address_confirmation=True,
                privacy_acknowledged=True,
            ),
            resolved_address=ResolvedLeadIntakeAddress(
                display_name="Wuse 2, Abuja, Nigeria",
                latitude=9.0765,
                longitude=7.3986,
                state="FCT",
                country_code="ng",
            ),
        ),
    )

    lead = db_session.get(Lead, submitted.lead_id)
    party = db_session.get(Party, submitted.party_id)
    origin = db_session.scalar(
        select(LeadOriginCapture).where(LeadOriginCapture.lead_id == lead.id)
    )
    assert submitted.lead_id == assessed.lead_id
    assert db_session.scalar(select(func.count(Lead.id))) == 1
    assert lead.metadata_["profile_completeness"] == "form_enriched"
    assert lead.metadata_["lead_intake_invitation_id"] == str(assessed.invitation_id)
    assert party.display_name == "Elite Iqraa Academy"
    assert party.party_type == "organization"
    assert (
        submitted.party_id
        != db_session.get(
            LeadIntakeInvitation, assessed.invitation_id
        ).representative_party_id
    )
    assert origin.capture_method == "inbox_classification"


def test_historical_resolved_classification_can_be_repaired_without_form(
    db_session,
):
    conversation, message = _instagram_conversation(db_session)
    conversation.status = "resolved"
    message.metadata_ = {
        **dict(message.metadata_ or {}),
        "ai_intake_status": "classified",
        "ai_intake_requires_follow_up": False,
        "ai_intent": "coverage_request",
        "ai_confidence": 0.97,
        "ai_party_type": "individual",
        "ai_party_type_confidence": 0.95,
        "ai_intake_provider": "pytest",
        "ai_intake_model": "classifier",
    }
    db_session.commit()

    findings = lead_intake.classified_candidate_drift(
        db_session,
        query=lead_intake.ClassifiedCandidateDriftQuery(
            since=datetime.now(UTC) - timedelta(days=60)
        ),
    )
    finding = next(item for item in findings if item.conversation_id == conversation.id)
    finding_conversation_id = finding.conversation_id
    finding_message_id = finding.message_id
    db_session.commit()
    outcome = lead_intake.assess_inbound(
        db_session,
        lead_intake.AssessInboundCommand(
            context=_context(f"historical-repair:{finding_message_id}"),
            conversation_id=finding_conversation_id,
            message_id=finding_message_id,
            classification=finding.classification,
            provider_label=finding.provider_label,
            model_label=finding.model_label,
            attribution=finding.attribution,
            allow_invitation=False,
        ),
    )

    assert outcome.action == "lead_created"
    assert outcome.lead_id is not None
    assert db_session.scalar(select(func.count(LeadIntakeInvitation.id))) == 0
    assert not any(
        item.conversation_id == conversation.id
        for item in lead_intake.classified_candidate_drift(
            db_session,
            query=lead_intake.ClassifiedCandidateDriftQuery(
                since=datetime.now(UTC) - timedelta(days=60)
            ),
        )
    )


def test_classified_candidate_event_invokes_typed_sales_handoff(
    db_session, monkeypatch
):
    conversation_id = uuid4()
    message_id = uuid4()
    captured = {}
    monkeypatch.setattr(
        lead_intake_event_handler, "finish_read_transaction", lambda _db: None
    )

    def _apply(_db, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(lead_intake_ai, "apply_shared_classification", _apply)
    event = Event(
        event_type=EventType.ai_intake_lead_candidate_classified,
        payload={
            "schema_version": 1,
            "tenant_id": str(OPERATOR_TENANT_ID),
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "classification": {
                "intent": "new_connection",
                "intent_confidence": 0.96,
                "party_type": "individual",
                "party_type_confidence": 0.94,
                "clarification_question": None,
            },
            "provider_label": "pytest",
            "model_label": "classifier",
            "attribution": {"external_ad_id": "ig-ad-1"},
        },
    )

    lead_intake_event_handler.LeadIntakeHandler().handle(db_session, event)

    assert captured["conversation_id"] == conversation_id
    assert captured["message_id"] == message_id
    assert captured["classification"].intent.value == "new_connection"
    assert captured["attribution"].external_ad_id == "ig-ad-1"


def test_classified_candidate_event_refuses_another_tenant(db_session, monkeypatch):
    monkeypatch.setattr(
        lead_intake_event_handler, "finish_read_transaction", lambda _db: None
    )
    applied = False

    def _apply(_db, **_kwargs):
        nonlocal applied
        applied = True

    monkeypatch.setattr(lead_intake_ai, "apply_shared_classification", _apply)
    event = Event(
        event_type=EventType.ai_intake_lead_candidate_classified,
        payload={
            "schema_version": 1,
            "tenant_id": str(uuid4()),
            "conversation_id": str(uuid4()),
            "message_id": str(uuid4()),
            "classification": {
                "intent": "new_connection",
                "intent_confidence": 0.96,
                "party_type": "individual",
                "party_type_confidence": 0.94,
                "clarification_question": None,
            },
            "provider_label": "pytest",
            "model_label": "classifier",
            "attribution": {},
        },
    )

    with pytest.raises(DomainError) as captured:
        lead_intake_event_handler.LeadIntakeHandler().handle(db_session, event)

    assert captured.value.code == "sales.lead_intake.event_tenant_mismatch"
    assert captured.value.retryable is False
    assert applied is False


def test_shared_metadata_handoff_runs_only_for_classified_sales(
    db_session, monkeypatch
):
    conversation, message = _conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    message.metadata_ = {
        **dict(message.metadata_ or {}),
        "ai_intake_status": "classified",
        "ai_intake_requires_follow_up": False,
        "ai_intent": "new_connection",
        "ai_confidence": 0.96,
        "ai_party_type": "individual",
        "ai_party_type_confidence": 0.94,
        "ai_intake_provider": "pytest",
        "ai_intake_model": "classifier",
    }
    db_session.commit()
    calls = []

    def _apply(_db, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(lead_intake_ai, "apply_shared_classification", _apply)

    lead_intake_ai.apply_inbox_intake_handoff(
        db_session, conversation_id=conversation_id, message_id=message_id
    )
    assert len(calls) == 1
    assert calls[0]["classification"].intent.value == "new_connection"

    message = db_session.get(InboxMessage, message_id)
    assert message is not None
    message.metadata_ = {
        **dict(message.metadata_ or {}),
        "ai_intent": "technical_support",
    }
    db_session.commit()
    lead_intake_ai.apply_inbox_intake_handoff(
        db_session, conversation_id=conversation_id, message_id=message_id
    )
    assert len(calls) == 1


def test_manual_form_completion_creates_party_first_lead_and_binds_inbox(db_session):
    staff, team = _staff_and_team(db_session)
    _published_template(db_session, staff=staff, team=team)
    conversation, message = _conversation(db_session)
    conversation_id = conversation.id
    message_id = message.id
    staff_id = staff.id
    team_id = team.id
    contact_address = conversation.contact_address
    db_session.commit()

    issued = lead_intake.issue_manual_invitation(
        db_session,
        lead_intake.ManualInvitationCommand(
            context=_context(f"invite:{conversation_id}"),
            conversation_id=conversation_id,
            trigger_message_id=message_id,
            party_type=LeadIntakePartyType.individual,
            actor_system_user_id=staff_id,
        ),
    )
    assert issued.token and issued.invitation_id
    invitation = db_session.get(LeadIntakeInvitation, issued.invitation_id)
    assert invitation is not None
    assert invitation.token_hash == lead_intake.token_hash(issued.token)
    assert issued.token not in invitation.token_hash
    invitation_id = invitation.id
    db_session.commit()

    outcome = lead_intake.submit_form(
        db_session,
        lead_intake.SubmitLeadIntakeCommand(
            context=_context(f"submit:{invitation_id}"),
            token=issued.token,
            submission=LeadIntakeSubmission(
                full_name="Amina Bello",
                gender="female",
                date_of_birth=date(1994, 5, 12),
                latitude=9.0765,
                longitude=7.3986,
                address_confirmation=True,
                privacy_acknowledged=True,
            ),
            resolved_address=ResolvedLeadIntakeAddress(
                display_name="Wuse 2, Abuja, Nigeria",
                latitude=9.0765,
                longitude=7.3986,
                state="FCT",
                country_code="ng",
            ),
        ),
    )

    lead = db_session.get(Lead, outcome.lead_id)
    party = db_session.get(Party, outcome.party_id)
    invitation = db_session.get(LeadIntakeInvitation, invitation_id)
    origin = db_session.scalar(
        select(LeadOriginCapture).where(LeadOriginCapture.lead_id == outcome.lead_id)
    )
    participant = db_session.scalar(
        select(InboxConversationParticipant).where(
            InboxConversationParticipant.conversation_id == conversation_id,
            InboxConversationParticipant.normalized_endpoint == contact_address,
        )
    )
    contact = db_session.get(PartyContactPoint, invitation.party_contact_point_id)
    role = db_session.scalar(
        select(PartyRole).where(
            PartyRole.party_id == party.id,
            PartyRole.role_type == "prospect",
        )
    )

    assert lead is not None and lead.subscriber_id is None
    assert party is not None and party.display_name == "Amina Bello"
    assert party.metadata_["state"] == "Federal Capital Territory"
    assert origin is not None
    assert origin.capture_method == "inbox_form"
    assert origin.source_platform == "team_inbox"
    assert invitation.status == "completed"
    assert invitation.lead_id == lead.id
    assert contact is not None and contact.consent_status == "unknown"
    assert participant.party_contact_point_id == contact.id
    assert participant.provider_account_scope == "phone-1"
    assert role is not None and role.status == "active"
    assert (
        db_session.get(InboxConversation, conversation_id).primary_service_team_id
        == team_id
    )
