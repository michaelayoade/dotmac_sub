from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.models.ai_intake import AiIntakeConfig
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import ai_inbox_automation, team_inbox_ai_automation


def test_default_inbox_ai_policy_is_dormant(db_session):
    policy = ai_inbox_automation.policy_from_config(None)
    state = ai_inbox_automation.effective_state(db_session)

    assert policy.scope_key == ai_inbox_automation.DEFAULT_SCOPE_KEY
    assert policy.is_enabled is False
    assert policy.auto_reply_enabled is False
    assert policy.auto_handoff_enabled is False
    assert state.may_classify is False
    assert state.may_send_customer_reply is False
    assert state.may_handoff is False


def test_effective_state_requires_provider_generation_and_intake(db_session):
    config = AiIntakeConfig(
        scope_key=ai_inbox_automation.DEFAULT_SCOPE_KEY,
        channel_type="chat_widget",
        is_enabled=True,
        auto_reply_enabled=False,
        auto_handoff_enabled=False,
        confidence_threshold=0.8,
        allow_followup_questions=True,
        max_clarification_turns=2,
        escalate_after_minutes=10,
        context_sources=["contact_identity", "account_health"],
        workflow_steps=[
            {
                "position": 1,
                "action": "classify_intent",
                "prompt": "Classify first.",
                "required_context": ["contact_identity"],
                "handoff_on_failure": True,
            }
        ],
        handoff_policy="live_agent",
    )
    db_session.add(config)
    db_session.flush()

    with (
        patch.object(ai_inbox_automation, "ai_enabled", lambda _db: True),
        patch.object(
            ai_inbox_automation.control_registry,
            "is_enabled",
            lambda _db, key: key == "ai.generation",
        ),
    ):
        state = ai_inbox_automation.effective_state(db_session)

    assert state.configured is True
    assert state.may_classify is True
    assert state.may_send_customer_reply is False
    assert state.may_handoff is False
    assert "customer replies and handoff remain disabled" in state.reason


def test_auto_reply_requires_its_own_switch(db_session):
    config = AiIntakeConfig(
        scope_key=ai_inbox_automation.DEFAULT_SCOPE_KEY,
        channel_type="chat_widget",
        is_enabled=True,
        auto_reply_enabled=True,
        auto_handoff_enabled=False,
        confidence_threshold=0.8,
        allow_followup_questions=True,
        max_clarification_turns=2,
        escalate_after_minutes=10,
        handoff_policy="manual_review",
    )
    db_session.add(config)
    db_session.flush()

    with (
        patch.object(ai_inbox_automation, "ai_enabled", lambda _db: True),
        patch.object(
            ai_inbox_automation.control_registry,
            "is_enabled",
            lambda _db, key: key == "ai.generation",
        ),
    ):
        state = ai_inbox_automation.effective_state(db_session)

    assert state.may_classify is True
    assert state.may_send_customer_reply is True


def test_conversation_context_does_not_guess_customer_without_link(db_session):
    conversation = InboxConversation(
        channel_type="chat_widget",
        contact_address="customer@example.com",
        status="open",
    )
    db_session.add(conversation)
    db_session.flush()
    policy = ai_inbox_automation.IntakePolicy(
        scope_key=ai_inbox_automation.DEFAULT_SCOPE_KEY,
        channel_type=ai_inbox_automation.IntakeChannel.chat_widget,
        is_enabled=False,
        auto_reply_enabled=False,
        auto_handoff_enabled=False,
        confidence_threshold=0.75,
        allow_followup_questions=True,
        max_clarification_turns=1,
        escalate_after_minutes=5,
        fallback_team_id=None,
        handoff_policy=ai_inbox_automation.HandoffPolicy.manual_review,
        assignment_strategy=ai_inbox_automation.AssignmentStrategy.available_round_robin,
        instructions=None,
        department_mappings=(),
        workflow_steps=ai_inbox_automation.default_workflow_steps(),
        context_sources=(ai_inbox_automation.ContextSourceKey.account_health,),
    )

    context = ai_inbox_automation.conversation_context(
        db_session, conversation_id=conversation.id, policy=policy
    )

    assert context.conversation_id == conversation.id
    assert context.subscriber_id is None
    assert context.account_health is None
    assert context.profile_missing_fields == ()
    assert context.access_paths == ()
    assert context.unavailable_sources == (
        ai_inbox_automation.ContextSourceKey.account_health,
    )


def test_policy_normalizes_workflow_and_department_mappings():
    team_id = uuid.uuid4()
    config = AiIntakeConfig(
        scope_key="inbox:default",
        channel_type="email",
        is_enabled=True,
        auto_reply_enabled=False,
        auto_handoff_enabled=True,
        confidence_threshold=0.7,
        allow_followup_questions=True,
        max_clarification_turns=1,
        escalate_after_minutes=5,
        department_mappings=[
            {"intent": "billing", "service_team_id": str(team_id), "label": "Billing"}
        ],
        workflow_steps=[
            {
                "position": 2,
                "action": "route_to_team",
                "prompt": "Route second.",
                "required_context": ["contact_identity"],
            },
            {
                "position": 1,
                "action": "classify_intent",
                "prompt": "Classify first.",
                "required_context": ["contact_identity"],
            },
        ],
        context_sources=["account_health", "account_health", "unknown"],
        handoff_policy="live_agent",
        assignment_strategy="available_round_robin",
    )

    policy = ai_inbox_automation.policy_from_config(config)

    assert [step.action.value for step in policy.workflow_steps] == [
        "classify_intent",
        "route_to_team",
    ]
    assert policy.department_mappings[0].service_team_id == team_id
    assert policy.auto_handoff_enabled is True
    assert (
        policy.assignment_strategy
        == ai_inbox_automation.AssignmentStrategy.available_round_robin
    )
    assert policy.context_sources == (
        ai_inbox_automation.ContextSourceKey.account_health,
    )


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _member(db_session, team: ServiceTeam):
    person_id = uuid.uuid4()
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person_id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=person_id,
            status=InboxAgentPresenceStatus.online.value,
        )
    )
    db_session.flush()
    return person_id


def _conversation_with_message(db_session, *, body: str = "My bill is wrong"):
    conversation = InboxConversation(
        channel_type="email",
        contact_address="customer@example.com",
        subject="Help",
        status="open",
    )
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction=InboxMessageDirection.inbound.value,
        subject="Help",
        body=body,
        from_address="customer@example.com",
        to_addresses=["support@example.com"],
    )
    db_session.add(message)
    db_session.flush()
    return conversation, message


def test_inbound_ai_automation_is_disabled_by_default(db_session):
    conversation, message = _conversation_with_message(db_session)
    db_session.flush()

    with patch.object(
        ai_inbox_automation,
        "classify_intake",
        side_effect=AssertionError("classification should not run"),
    ):
        outcome = team_inbox_ai_automation.apply_inbound_ai_intake(
            db_session,
            conversation_id=conversation.id,
            message_id=message.id,
        )

    assert outcome.kind == "skipped"


def test_inbound_ai_automation_classifies_and_assigns_available_agent(db_session):
    team = _team(db_session, "Billing")
    agent = _member(db_session, team)
    conversation, message = _conversation_with_message(db_session)
    db_session.add(
        AiIntakeConfig(
            scope_key=ai_inbox_automation.DEFAULT_SCOPE_KEY,
            channel_type="email",
            is_enabled=True,
            auto_reply_enabled=False,
            auto_handoff_enabled=True,
            confidence_threshold=0.75,
            allow_followup_questions=True,
            max_clarification_turns=1,
            escalate_after_minutes=5,
            fallback_team_id=team.id,
            department_mappings=[
                {
                    "intent": "billing",
                    "service_team_id": str(team.id),
                    "label": "Billing",
                }
            ],
            handoff_policy="live_agent",
            assignment_strategy="available_round_robin",
        )
    )
    db_session.flush()
    decision = ai_inbox_automation.IntakeDecision(
        intent="billing",
        confidence=0.96,
        has_enough_information=True,
        should_handoff=True,
        target_service_team_id=team.id,
        followup_question=None,
        customer_reply=None,
        should_close=False,
        rationale="Billing request.",
        raw={},
    )

    with (
        patch.object(ai_inbox_automation, "ai_enabled", lambda _db: True),
        patch.object(
            ai_inbox_automation.control_registry,
            "is_enabled",
            lambda _db, key: key == "ai.generation",
        ),
        patch.object(ai_inbox_automation, "classify_intake", return_value=decision),
    ):
        outcome = team_inbox_ai_automation.apply_inbound_ai_intake(
            db_session,
            conversation_id=conversation.id,
            message_id=message.id,
        )

    assignment = db_session.query(InboxConversationAssignment).one()
    assert outcome.kind == "handed_off"
    assert outcome.assigned_person_id == str(agent)
    assert assignment.person_id == agent
    assert conversation.primary_service_team_id == team.id
    assert conversation.metadata_["last_ai_intake"]["intent"] == "billing"


def test_inbound_ai_automation_asks_followup_when_information_is_missing(db_session):
    conversation, message = _conversation_with_message(db_session, body="Help")
    db_session.add(
        AiIntakeConfig(
            scope_key=ai_inbox_automation.DEFAULT_SCOPE_KEY,
            channel_type="email",
            is_enabled=True,
            auto_reply_enabled=True,
            auto_handoff_enabled=False,
            confidence_threshold=0.75,
            allow_followup_questions=True,
            max_clarification_turns=1,
            escalate_after_minutes=5,
            handoff_policy="manual_review",
            assignment_strategy="available_round_robin",
        )
    )
    db_session.flush()
    decision = ai_inbox_automation.IntakeDecision(
        intent="unknown",
        confidence=0.42,
        has_enough_information=False,
        should_handoff=False,
        target_service_team_id=None,
        followup_question="Please share your account number so we can help.",
        customer_reply=None,
        should_close=False,
        rationale="Missing account context.",
        raw={},
    )
    replies: list[str] = []

    def fake_reply(_db, *, conversation, payload, **_kwargs):
        replies.append(payload.body_text)
        return SimpleNamespace(kind="queued", assigned_person_id=None)

    with (
        patch.object(ai_inbox_automation, "ai_enabled", lambda _db: True),
        patch.object(
            ai_inbox_automation.control_registry,
            "is_enabled",
            lambda _db, key: key == "ai.generation",
        ),
        patch.object(ai_inbox_automation, "classify_intake", return_value=decision),
        patch.object(
            team_inbox_ai_automation.team_inbox_outbound,
            "send_inbox_reply",
            side_effect=fake_reply,
        ),
    ):
        outcome = team_inbox_ai_automation.apply_inbound_ai_intake(
            db_session,
            conversation_id=conversation.id,
            message_id=message.id,
        )

    assert outcome.kind == "asked_followup"
    assert replies == ["Please share your account number so we can help."]
    assert conversation.metadata_["last_ai_intake"]["reply_kind"] == "queued"
