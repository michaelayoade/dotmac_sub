"""Behavior contracts for shared customer-facing Inbox AI intake."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.ai_intake import AiIntakeConfig, CustomerAiIntakeAssessment
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
)
from app.services.ai.client import AIClientError, AIResponse
from app.services.crm import ai_intake


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=f"{name} {uuid4().hex[:8]}", team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _configured_inbox(db_session, *, channel: str = "whatsapp"):
    original = _team(db_session, "Default", ServiceTeamType.support.value)
    technical = _team(db_session, "Technical", ServiceTeamType.support.value)
    helpdesk = _team(db_session, "Helpdesk", ServiceTeamType.support.value)
    sales = _team(db_session, "Sales", ServiceTeamType.operations.value)
    fallback = _team(db_session, "Fallback", ServiceTeamType.support.value)
    config = AiIntakeConfig(
        scope_key=f"inbox:{channel}",
        channel_type=channel,
        is_enabled=True,
        confidence_threshold=0.75,
        allow_followup_questions=True,
        max_clarification_turns=1,
        escalate_after_minutes=5,
        fallback_team_id=fallback.id,
        department_mappings=[
            {
                "department": "technical_support",
                "service_team_id": str(technical.id),
            },
            {"department": "helpdesk", "service_team_id": str(helpdesk.id)},
            {"department": "sales", "service_team_id": str(sales.id)},
        ],
    )
    db_session.add(config)
    db_session.commit()
    return original, technical, helpdesk, sales, fallback, config


def _message(
    db_session,
    *,
    channel: str,
    team: ServiceTeam,
    body: str,
    conversation: InboxConversation | None = None,
) -> tuple[InboxConversation, InboxMessage]:
    if conversation is None:
        conversation = InboxConversation(
            channel_type=channel,
            primary_service_team_id=team.id,
            contact_address=f"contact-{uuid4().hex[:10]}",
            external_thread_id=f"thread-{uuid4().hex}",
            status="open",
            priority=73,
            first_message_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
            metadata_={"contact_resolution": {"status": "unmatched"}},
        )
        db_session.add(conversation)
        db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=channel,
        direction="inbound",
        body=body,
        from_address=conversation.contact_address,
        external_message_id=f"message-{uuid4().hex}",
        metadata_={
            "provider": "meta_cloud_api" if channel == "whatsapp" else "meta_social",
            "provider_account_scope": "account-1",
        },
    )
    db_session.add(message)
    db_session.commit()
    return conversation, message


def _classification(
    intent: str,
    category: str,
    department: str,
    *,
    confidence: float = 0.95,
    requires_follow_up: bool = False,
    follow_up_question: str | None = None,
    party_type: str = "unknown",
    party_type_confidence: float = 0.0,
) -> dict[str, object]:
    return {
        "intent": intent,
        "category": category,
        "confidence": confidence,
        "department": department,
        "requires_follow_up": requires_follow_up,
        "follow_up_question": follow_up_question,
        "summary": "Controlled operational summary",
        "party_type": party_type,
        "party_type_confidence": party_type_confidence,
    }


def _gateway(monkeypatch, *payloads: dict[str, object] | Exception):
    calls: list[dict[str, object]] = []
    remaining = list(payloads)

    def generate(_db, **kwargs):
        calls.append(kwargs)
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return (
            AIResponse(
                content=json.dumps(item),
                tokens_in=20,
                tokens_out=30,
                model="test-model",
                provider="test-provider",
            ),
            {"endpoint": "primary", "fallback_used": False},
        )

    monkeypatch.setattr(ai_intake.ai_gateway, "generate_with_fallback", generate)
    return calls


@pytest.mark.parametrize(
    ("body", "intent", "category"),
    [
        ("My internet is off", "technical_support", "no_internet"),
        ("My internet is slow", "technical_support", "slow_internet"),
        ("It keeps going on and off", "technical_support", "intermittent_connection"),
        ("The router has a red light", "technical_support", "router_issue"),
    ],
)
def test_technical_categories_route_to_technical_support(
    db_session, monkeypatch, body, intent, category
):
    original, technical, _helpdesk, _sales, _fallback, _config = (
        _configured_inbox(db_session)
    )
    conversation, message = _message(
        db_session, channel="whatsapp", team=original, body=body
    )
    _gateway(monkeypatch, _classification(intent, category, "technical_support"))
    sales_calls: list[object] = []
    monkeypatch.setattr(
        ai_intake,
        "_sales_handoff",
        lambda *args, **kwargs: sales_calls.append((args, kwargs)),
    )

    outcome = ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=message.id
    )

    db_session.refresh(conversation)
    assert outcome is not None and outcome.status == "routed"
    assert conversation.primary_service_team_id == technical.id
    assert sales_calls == []
    assert db_session.query(InboxConversationAssignment).count() == 0
    assert conversation.priority == 73
    assert conversation.first_message_at == datetime(2026, 8, 4, 10, 0)


@pytest.mark.parametrize(
    ("intent", "category"),
    [
        ("billing", "billing_issue"),
        ("payment_confirmation", "payment_not_reflected"),
        ("subscription", "subscription_renewal"),
        ("subscription", "plan_change"),
        ("account_access", "account_login_issue"),
        ("general_complaint", "general_complaint"),
        ("general_enquiry", "general_enquiry"),
    ],
)
def test_helpdesk_categories_route_to_helpdesk(
    db_session, monkeypatch, intent, category
):
    original, _technical, helpdesk, _sales, _fallback, _config = (
        _configured_inbox(db_session)
    )
    conversation, message = _message(
        db_session, channel="whatsapp", team=original, body="Please help"
    )
    _gateway(monkeypatch, _classification(intent, category, "helpdesk"))
    monkeypatch.setattr(ai_intake, "_sales_handoff", lambda *args, **kwargs: None)

    ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=message.id
    )

    db_session.refresh(conversation)
    assert conversation.primary_service_team_id == helpdesk.id


@pytest.mark.parametrize("category", ["coverage_request", "new_connection_request"])
def test_new_connection_hands_off_to_sales_only(db_session, monkeypatch, category):
    original, _technical, _helpdesk, sales, _fallback, _config = (
        _configured_inbox(db_session)
    )
    conversation, message = _message(
        db_session,
        channel="whatsapp",
        team=original,
        body="I need internet at my house in Mararaba",
    )
    _gateway(
        monkeypatch,
        _classification(
            "new_connection",
            category,
            "sales",
            party_type="individual",
            party_type_confidence=0.94,
        ),
    )
    handoffs: list[dict[str, object]] = []
    monkeypatch.setattr(
        ai_intake,
        "_sales_handoff",
        lambda *args, **kwargs: handoffs.append(kwargs),
    )

    ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=message.id
    )

    db_session.refresh(conversation)
    assert conversation.primary_service_team_id == sales.id
    assert len(handoffs) == 1
    assert handoffs[0]["message_id"] == message.id


def test_disabled_config_and_unsupported_email_never_call_ai(db_session, monkeypatch):
    original, _technical, _helpdesk, _sales, fallback, config = _configured_inbox(
        db_session
    )
    config.is_enabled = False
    db_session.commit()
    conversation, message = _message(
        db_session, channel="whatsapp", team=original, body="Internet is off"
    )
    calls = _gateway(monkeypatch)

    disabled = ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=message.id
    )

    db_session.refresh(conversation)
    assert disabled is not None and disabled.status == "disabled"
    assert conversation.primary_service_team_id == fallback.id
    assert calls == []

    email_config = AiIntakeConfig(
        scope_key="inbox:email",
        channel_type="email",
        is_enabled=True,
        fallback_team_id=fallback.id,
    )
    db_session.add(email_config)
    db_session.commit()
    email_conversation, email_message = _message(
        db_session, channel="email", team=original, body="Payment issue"
    )
    unsupported = ai_intake.classify_and_route(
        db_session,
        conversation_id=email_conversation.id,
        message_id=email_message.id,
    )
    assert unsupported is not None and unsupported.status == "unsupported_channel"
    assert calls == []


def test_low_confidence_asks_once_then_uses_fallback(db_session, monkeypatch):
    original, _technical, _helpdesk, _sales, fallback, _config = _configured_inbox(
        db_session
    )
    conversation, first = _message(
        db_session,
        channel="whatsapp",
        team=original,
        body="Please help me. It is not working.",
    )
    unclear = _classification(
        "unknown",
        "unknown",
        "fallback",
        confidence=0.4,
        requires_follow_up=True,
        follow_up_question="request_type",
    )
    calls = _gateway(monkeypatch, unclear, unclear)
    replies: list[dict[str, object]] = []
    monkeypatch.setattr(
        ai_intake.team_inbox_commands,
        "reply",
        lambda *args, **kwargs: replies.append(kwargs),
    )

    first_outcome = ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=first.id
    )
    _conversation, second = _message(
        db_session,
        channel="whatsapp",
        team=original,
        body="I still cannot explain it",
        conversation=conversation,
    )
    second_outcome = ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=second.id
    )

    db_session.refresh(conversation)
    assert first_outcome is not None and first_outcome.status == "follow_up_sent"
    assert second_outcome is not None and second_outcome.status == "fallback"
    assert len(replies) == 1
    assert replies[0]["body_text"] == (
        "Is this about a technical problem, billing or account help, or getting "
        "a new internet connection?"
    )
    assert conversation.primary_service_team_id == fallback.id
    assert len(calls) == 2


def test_ai_failure_routes_safely_and_duplicate_skips_provider(
    db_session, monkeypatch
):
    original, _technical, _helpdesk, _sales, fallback, _config = _configured_inbox(
        db_session
    )
    conversation, message = _message(
        db_session, channel="whatsapp", team=original, body="Payment missing"
    )
    calls = _gateway(
        monkeypatch,
        AIClientError("provider timeout", failure_type="timeout", transient=True),
    )

    first = ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=message.id
    )
    second = ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=message.id
    )

    db_session.refresh(conversation)
    assert first is not None and first.status == "ai_failed"
    assert second is not None and second.replayed is True
    assert conversation.primary_service_team_id == fallback.id
    assert len(calls) == 1
    assert (
        db_session.query(CustomerAiIntakeAssessment)
        .filter(CustomerAiIntakeAssessment.message_id == message.id)
        .count()
        == 1
    )


def test_assigned_conversation_is_not_moved_or_reassigned(db_session, monkeypatch):
    original, technical, _helpdesk, _sales, _fallback, _config = (
        _configured_inbox(db_session)
    )
    conversation, message = _message(
        db_session, channel="whatsapp", team=original, body="Internet is off"
    )
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=original.id,
        person_id=uuid4(),
        is_active=True,
    )
    db_session.add(assignment)
    db_session.commit()
    _gateway(
        monkeypatch,
        _classification("technical_support", "no_internet", "technical_support"),
    )

    outcome = ai_intake.classify_and_route(
        db_session, conversation_id=conversation.id, message_id=message.id
    )

    db_session.refresh(conversation)
    db_session.refresh(assignment)
    assert outcome is not None and outcome.route_result == "preserved_assigned"
    assert conversation.primary_service_team_id == original.id
    assert conversation.primary_service_team_id != technical.id
    assert assignment.is_active is True
    assert db_session.query(InboxConversationAssignment).count() == 1
