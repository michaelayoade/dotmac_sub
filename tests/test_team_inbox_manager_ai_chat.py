from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.team_inbox import InboxConversation, InboxMessage
from app.services import team_inbox_manager_ai_chat
from app.services.ai.client import AIClientError, AIResponse


def _conversation(db_session):
    conversation = InboxConversation(
        channel_type="email",
        subject="Slow connection",
        contact_address="customer@example.test",
        status="open",
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="inbound",
                subject="Slow connection",
                body="My internet is slow every evening.",
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction="outbound",
                subject="Re: Slow connection",
                body="Please confirm whether the LOS light is red.",
            ),
        ]
    )
    db_session.flush()
    return conversation


def test_manager_ai_chat_requires_generation_gate(db_session):
    conversation = _conversation(db_session)

    with patch.object(
        team_inbox_manager_ai_chat.control_registry,
        "is_enabled",
        return_value=False,
    ):
        with pytest.raises(AIClientError):
            team_inbox_manager_ai_chat.answer_manager_question(
                db_session,
                question="What is happening?",
                conversation_id=conversation.id,
            )


def test_manager_ai_chat_builds_conversation_context(db_session):
    conversation = _conversation(db_session)
    captured = {}

    def fake_generate(_db, **kwargs):
        captured.update(kwargs)
        return (
            AIResponse(
                content="The customer reports evening slowness and needs LOS confirmation.",
                tokens_in=10,
                tokens_out=12,
                model="test",
                provider="fake",
            ),
            {"endpoint": "primary"},
        )

    with (
        patch.object(
            team_inbox_manager_ai_chat.control_registry,
            "is_enabled",
            return_value=True,
        ),
        patch.object(
            team_inbox_manager_ai_chat.ai_gateway, "enabled", return_value=True
        ),
        patch.object(
            team_inbox_manager_ai_chat.ai_gateway,
            "generate_with_fallback",
            side_effect=fake_generate,
        ),
    ):
        answer = team_inbox_manager_ai_chat.answer_manager_question(
            db_session,
            question="Summarize risk and next step.",
            conversation_id=conversation.id,
        )

    assert "evening slowness" in answer
    assert "Summarize risk and next step." in captured["prompt"]
    assert "My internet is slow every evening." in captured["prompt"]
    assert "Do not claim you performed assignments" in captured["system"]


def test_manager_ai_page_state_lists_recent_conversations(db_session):
    conversation = _conversation(db_session)

    with (
        patch.object(
            team_inbox_manager_ai_chat.ai_gateway, "enabled", return_value=True
        ),
        patch.object(
            team_inbox_manager_ai_chat.control_registry,
            "is_enabled",
            return_value=True,
        ),
    ):
        state = team_inbox_manager_ai_chat.build_page_state(
            db_session, conversation_id=conversation.id
        )

    assert state.provider_enabled is True
    assert state.generation_enabled is True
    assert state.selected_conversation_id == conversation.id
    assert state.conversations[0].id == conversation.id


def test_manager_ai_permission_constant_is_stable():
    assert team_inbox_manager_ai_chat.MANAGER_AI_PERMISSION == "support:inbox_ai:read"
