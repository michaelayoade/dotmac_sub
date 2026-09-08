from __future__ import annotations

from app.models.team_inbox import InboxConversation, InboxMessage
from app.services import team_inbox_maintenance


def test_meta_name_repair_preview_excludes_already_named_conversations(db_session):
    missing = InboxConversation(
        channel_type="instagram_dm",
        status="open",
        contact_address="igsid-missing",
        external_thread_id="instagram_dm:igsid-missing",
        is_active=True,
        metadata_={},
    )
    named = InboxConversation(
        channel_type="facebook_messenger",
        status="open",
        contact_address="psid-named",
        external_thread_id="facebook_messenger:psid-named",
        is_active=True,
        metadata_={"contact_name": "Known Person"},
    )
    db_session.add_all((missing, named))
    db_session.commit()

    preview = team_inbox_maintenance.preview_meta_profile_repairs(db_session)

    assert tuple(row.conversation_id for row in preview.candidates) == (missing.id,)
    assert len(preview.digest) == 64


def test_failed_meta_delivery_preview_never_selects_email(db_session):
    meta_conversation = InboxConversation(
        channel_type="facebook_messenger",
        status="open",
        contact_address="psid-1",
        external_thread_id="facebook_messenger:psid-1",
        is_active=True,
    )
    email_conversation = InboxConversation(
        channel_type="email",
        status="open",
        contact_address="person@example.test",
        external_thread_id="email:person@example.test",
        is_active=True,
    )
    db_session.add_all((meta_conversation, email_conversation))
    db_session.flush()
    meta_message = InboxMessage(
        conversation_id=meta_conversation.id,
        channel_type="facebook_messenger",
        direction="outbound",
        body="Meta reply",
        metadata_={"delivery_status": "failed", "retry_count": 1},
    )
    email_message = InboxMessage(
        conversation_id=email_conversation.id,
        channel_type="email",
        direction="outbound",
        body="Email reply",
        metadata_={"delivery_status": "failed", "retry_count": 1},
    )
    db_session.add_all((meta_message, email_message))
    db_session.commit()

    preview = team_inbox_maintenance.preview_failed_meta_deliveries(db_session)

    assert tuple(row.message_id for row in preview.candidates) == (meta_message.id,)
