"""Tests for Meta webhooks service."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models.crm.conversation import Conversation, Message
from app.models.crm.enums import ChannelType, MessageDirection, MessageStatus
from app.models.person import Person, PersonChannel, ChannelType as PersonChannelType
from app.schemas.crm.inbox import MetaWebhookPayload
from app.services import meta_webhooks


# =============================================================================
# Webhook Signature Verification Tests
# =============================================================================


def test_verify_webhook_signature_valid():
    """Test valid webhook signature verification."""
    body = b'{"object":"page","entry":[]}'
    app_secret = "test_secret_123"

    # Generate valid signature
    expected_sig = hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    signature = f"sha256={expected_sig}"

    result = meta_webhooks.verify_webhook_signature(body, signature, app_secret)
    assert result is True


def test_verify_webhook_signature_invalid():
    """Test invalid webhook signature is rejected."""
    body = b'{"object":"page","entry":[]}'
    app_secret = "test_secret_123"

    # Wrong signature
    signature = "sha256=invalid_signature_abc123"

    result = meta_webhooks.verify_webhook_signature(body, signature, app_secret)
    assert result is False


def test_verify_webhook_signature_missing():
    """Test missing signature is rejected."""
    body = b'{"object":"page","entry":[]}'
    app_secret = "test_secret_123"

    result = meta_webhooks.verify_webhook_signature(body, None, app_secret)
    assert result is False


def test_verify_webhook_signature_wrong_format():
    """Test signature without sha256= prefix is rejected."""
    body = b'{"object":"page","entry":[]}'
    app_secret = "test_secret_123"

    # Missing sha256= prefix
    signature = "just_a_hash_without_prefix"

    result = meta_webhooks.verify_webhook_signature(body, signature, app_secret)
    assert result is False


def test_fetch_profile_name_logs_auth_failure(caplog):
    """Auth failures should be logged and return None."""
    response = httpx.Response(
        403,
        request=httpx.Request("GET", "https://graph.facebook.com/v1.0/user"),
        json={"error": {"message": "Forbidden"}},
    )
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = None

    with patch("httpx.Client", return_value=client):
        with caplog.at_level("WARNING"):
            result = meta_webhooks._fetch_profile_name(
                "token",
                "user",
                "name",
                "https://graph.facebook.com",
            )

    assert result is None
    assert "meta_profile_lookup_auth_failed" in caplog.text


# =============================================================================
# Process Messenger Webhook Tests
# =============================================================================


def test_process_messenger_webhook_message(db_session):
    """Test processing incoming Messenger message webhook."""
    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "user_456"},
                        "recipient": {"id": "page_123"},
                        "timestamp": 1704067200000,
                        "message": {
                            "mid": "m_abc123",
                            "text": "Hello, I need help!",
                        },
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_facebook_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_messenger_webhook(db_session, payload)

        mock_receive.assert_called_once()
        call_args = mock_receive.call_args
        # First positional arg is db_session, second is the parsed payload
        fb_payload = call_args[0][1]
        assert fb_payload.page_id == "page_123"
        assert fb_payload.contact_address == "user_456"
        assert fb_payload.body == "Hello, I need help!"
        assert fb_payload.message_id == "m_abc123"


def test_process_messenger_webhook_multiple_messages(db_session):
    """Test processing multiple messages in one webhook."""
    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "user_1"},
                        "recipient": {"id": "page_123"},
                        "timestamp": 1704067200000,
                        "message": {"mid": "m_1", "text": "First message"},
                    },
                    {
                        "sender": {"id": "user_2"},
                        "recipient": {"id": "page_123"},
                        "timestamp": 1704067201000,
                        "message": {"mid": "m_2", "text": "Second message"},
                    },
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_facebook_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_messenger_webhook(db_session, payload)

        assert mock_receive.call_count == 2


def test_process_messenger_webhook_delivery_receipt(db_session):
    """Test processing delivery receipt (not a message)."""
    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "page_123"},
                        "recipient": {"id": "user_456"},
                        "timestamp": 1704067200000,
                        "delivery": {
                            "mids": ["m_abc123"],
                            "watermark": 1704067200000,
                        },
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_facebook_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_messenger_webhook(db_session, payload)

        # Delivery receipts should not create messages
        mock_receive.assert_not_called()


def test_process_messenger_webhook_read_receipt_updates_read_at(db_session):
    """Read receipts should mark inbound messages as read."""
    person = Person(
        first_name="Read",
        last_name="Receipt",
        email="read.receipt@example.com",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    channel = PersonChannel(
        person_id=person.id,
        channel_type=PersonChannelType.facebook_messenger,
        address="user_456",
        is_primary=True,
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)

    conv = Conversation(person_id=person.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)

    received_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    msg = Message(
        conversation_id=conv.id,
        person_channel_id=channel.id,
        channel_type=ChannelType.facebook_messenger,
        direction=MessageDirection.inbound,
        status=MessageStatus.received,
        received_at=received_at,
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "page_123"},
                        "recipient": {"id": "user_456"},
                        "timestamp": 1704067200000,
                        "read": {"watermark": 1704067200000},
                    }
                ],
            }
        ],
    )

    meta_webhooks.process_messenger_webhook(db_session, payload)
    db_session.refresh(msg)
    assert msg.read_at is not None


def test_process_messenger_webhook_empty_entry(db_session):
    """Test processing webhook with empty entry."""
    payload = MetaWebhookPayload(object="page", entry=[])

    results = meta_webhooks.process_messenger_webhook(db_session, payload)
    assert results == []


# =============================================================================
# Process Instagram Webhook Tests
# =============================================================================


def test_process_instagram_webhook_message(db_session):
    """Test processing incoming Instagram DM webhook."""
    payload = MetaWebhookPayload(
        object="instagram",
        entry=[
            {
                "id": "ig_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "ig_user_456"},
                        "recipient": {"id": "ig_123"},
                        "timestamp": 1704067200000,
                        "message": {
                            "mid": "ig_m_abc123",
                            "text": "Hi there via Instagram!",
                        },
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_instagram_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_instagram_webhook(db_session, payload)

        mock_receive.assert_called_once()
        call_args = mock_receive.call_args
        # First positional arg is db_session, second is the parsed payload
        ig_payload = call_args[0][1]
        assert ig_payload.instagram_account_id == "ig_123"
        assert ig_payload.contact_address == "ig_user_456"
        assert ig_payload.body == "Hi there via Instagram!"
        assert ig_payload.message_id == "ig_m_abc123"


def test_process_instagram_webhook_story_mention(db_session):
    """Test processing Instagram story mention webhook."""
    payload = MetaWebhookPayload(
        object="instagram",
        entry=[
            {
                "id": "ig_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "ig_user_456"},
                        "recipient": {"id": "ig_123"},
                        "timestamp": 1704067200000,
                        "message": {
                            "mid": "ig_m_story",
                            "attachments": [
                                {
                                    "type": "story_mention",
                                    "payload": {"url": "https://instagram.com/story"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_instagram_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_instagram_webhook(db_session, payload)

        # Story mentions should still be processed (attachment handling)
        mock_receive.assert_called_once()


# =============================================================================
# Receive Facebook Message Tests
# =============================================================================


def test_receive_facebook_message_creates_contact(db_session):
    """Test receiving Facebook message creates contact and conversation."""
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    payload = FacebookMessengerWebhookPayload(
        contact_address="fb_user_456",
        message_id="m_abc123",
        page_id="page_123",
        body="Hello from Messenger!",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    message = meta_webhooks.receive_facebook_message(db_session, payload)

    assert message is not None
    assert message.body == "Hello from Messenger!"
    assert message.channel_type == ChannelType.facebook_messenger
    assert message.direction == MessageDirection.inbound
    assert message.status == MessageStatus.received
    assert message.external_id == "m_abc123"
    assert message.metadata_.get("page_id") == "page_123"


def test_receive_facebook_message_existing_contact(db_session):
    """Test receiving message uses existing contact."""
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    # First message creates contact
    payload1 = FacebookMessengerWebhookPayload(
        contact_address="fb_repeat_user",
        message_id="m_1",
        page_id="page_123",
        body="First message",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    message1 = meta_webhooks.receive_facebook_message(db_session, payload1)
    person_id = message1.conversation.person_id

    # Second message uses same contact
    payload2 = FacebookMessengerWebhookPayload(
        contact_address="fb_repeat_user",
        message_id="m_2",
        page_id="page_123",
        body="Second message",
        received_at=datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    message2 = meta_webhooks.receive_facebook_message(db_session, payload2)

    assert message2.conversation.person_id == person_id


def test_receive_facebook_message_missing_mid_dedupes(db_session):
    """Test missing Messenger message_id is deduped deterministically."""
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    payload = FacebookMessengerWebhookPayload(
        contact_address="fb_no_mid_user",
        message_id=None,
        page_id="page_999",
        body="Same message without MID",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    first = meta_webhooks.receive_facebook_message(db_session, payload)
    second = meta_webhooks.receive_facebook_message(db_session, payload)

    assert first.id == second.id


def test_receive_facebook_message_with_attachment(db_session):
    """Test receiving message with attachment metadata."""
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    attachments = [{"type": "image", "payload": {"url": "https://cdn.com/image.jpg"}}]

    payload = FacebookMessengerWebhookPayload(
        contact_address="fb_user_789",
        message_id="m_attach",
        page_id="page_123",
        body="Check this image",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        metadata={"attachments": attachments},
    )

    message = meta_webhooks.receive_facebook_message(db_session, payload)

    assert message is not None
    assert message.body == "Check this image"
    assert message.metadata_.get("attachments") == attachments
    assert message.metadata_.get("page_id") == "page_123"


# =============================================================================
# Receive Instagram Message Tests
# =============================================================================


def test_receive_instagram_message_creates_contact(db_session):
    """Test receiving Instagram DM creates contact and conversation."""
    from app.schemas.crm.inbox import InstagramDMWebhookPayload

    payload = InstagramDMWebhookPayload(
        contact_address="ig_user_456",
        message_id="ig_m_abc123",
        instagram_account_id="ig_123",
        body="Hello from Instagram!",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    message = meta_webhooks.receive_instagram_message(db_session, payload)

    assert message is not None
    assert message.body == "Hello from Instagram!"
    assert message.channel_type == ChannelType.instagram_dm
    assert message.direction == MessageDirection.inbound
    assert message.status == MessageStatus.received
    assert message.external_id == "ig_m_abc123"
    assert message.metadata_.get("instagram_account_id") == "ig_123"


def test_receive_instagram_message_existing_conversation(db_session):
    """Test receiving Instagram message uses existing open conversation."""
    from app.schemas.crm.inbox import InstagramDMWebhookPayload

    # First message creates conversation
    payload1 = InstagramDMWebhookPayload(
        contact_address="ig_repeat_user",
        message_id="ig_m_1",
        instagram_account_id="ig_123",
        body="First IG message",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    message1 = meta_webhooks.receive_instagram_message(db_session, payload1)
    conversation_id = message1.conversation_id

    # Second message uses same conversation
    payload2 = InstagramDMWebhookPayload(
        contact_address="ig_repeat_user",
        message_id="ig_m_2",
        instagram_account_id="ig_123",
        body="Second IG message",
        received_at=datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    message2 = meta_webhooks.receive_instagram_message(db_session, payload2)

    assert message2.conversation_id == conversation_id


def test_receive_instagram_message_missing_mid_dedupes(db_session):
    """Test missing Instagram message_id is deduped deterministically."""
    from app.schemas.crm.inbox import InstagramDMWebhookPayload

    payload = InstagramDMWebhookPayload(
        contact_address="ig_no_mid_user",
        message_id=None,
        instagram_account_id="ig_999",
        body="Same IG message without MID",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    first = meta_webhooks.receive_instagram_message(db_session, payload)
    second = meta_webhooks.receive_instagram_message(db_session, payload)

    assert first.id == second.id


def test_receive_facebook_message_links_by_email(db_session):
    """FB messages should reuse existing person when email is provided."""
    from app.models.person import Person
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    person = Person(
        first_name="Test",
        last_name="User",
        email="fb-link@example.com",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    initial_count = db_session.query(Person).count()
    payload = FacebookMessengerWebhookPayload(
        contact_address="fb_user_1",
        message_id="m_fb_1",
        page_id="page_123",
        body="Hi from FB",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        metadata={"email": "fb-link@example.com"},
    )

    message = meta_webhooks.receive_facebook_message(db_session, payload)

    assert message.conversation.person_id == person.id
    assert db_session.query(Person).count() == initial_count


def test_receive_facebook_message_reuses_person_across_sender_ids(db_session):
    """FB messages should not create duplicate people for different sender IDs."""
    from app.models.person import Person
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    person = Person(
        first_name="Test",
        last_name="User",
        email="fb-dupe@example.com",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    initial_count = db_session.query(Person).count()
    payload1 = FacebookMessengerWebhookPayload(
        contact_address="fb_user_page1",
        message_id="m_fb_page1",
        page_id="page_123",
        body="Hi from page 1",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        metadata={"email": "fb-dupe@example.com"},
    )
    payload2 = FacebookMessengerWebhookPayload(
        contact_address="fb_user_page2",
        message_id="m_fb_page2",
        page_id="page_456",
        body="Hi from page 2",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        metadata={"email": "fb-dupe@example.com"},
    )

    message1 = meta_webhooks.receive_facebook_message(db_session, payload1)
    message2 = meta_webhooks.receive_facebook_message(db_session, payload2)

    assert message1.conversation.person_id == person.id
    assert message2.conversation.person_id == person.id
    assert db_session.query(Person).count() == initial_count


def test_receive_facebook_message_links_by_account(db_session, subscriber_account):
    """FB messages should reuse account-linked person when account_id is provided."""
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    payload = FacebookMessengerWebhookPayload(
        contact_address="fb_user_2",
        message_id="m_fb_2",
        page_id="page_123",
        body="Hi from FB account",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        metadata={"account_id": str(subscriber_account.id)},
    )

    message = meta_webhooks.receive_facebook_message(db_session, payload)

    assert message.conversation.person_id == subscriber_account.subscriber.person_id


def test_receive_facebook_message_applies_account_from_email_metadata(
    db_session,
    subscriber_account,
):
    """FB messages should apply account metadata when email links to an account."""
    from app.models.subscriber import AccountRole, AccountRoleType
    from app.schemas.crm.inbox import FacebookMessengerWebhookPayload

    person = subscriber_account.subscriber.person
    person.email = "fb-account@example.com"
    db_session.add(
        AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
    )
    db_session.commit()
    db_session.refresh(person)

    payload = FacebookMessengerWebhookPayload(
        contact_address="fb_user_3",
        message_id="m_fb_3",
        page_id="page_123",
        body="Hi from FB account via email",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        metadata={"email": "fb-account@example.com"},
    )

    message = meta_webhooks.receive_facebook_message(db_session, payload)

    db_session.refresh(person)
    assert message.conversation.person_id == person.id
    assert person.metadata_.get("account_id") == str(subscriber_account.id)


# =============================================================================
# Edge Case Tests
# =============================================================================


def test_process_webhook_with_echo_message(db_session):
    """Test that echo messages (messages from page) are ignored."""
    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "page_123"},  # Page is sender (echo)
                        "recipient": {"id": "user_456"},
                        "timestamp": 1704067200000,
                        "message": {
                            "mid": "m_echo",
                            "text": "Reply from page",
                            "is_echo": True,
                        },
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_facebook_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_messenger_webhook(db_session, payload)

        # Echo messages should be ignored
        mock_receive.assert_not_called()


def test_process_webhook_skips_self_sender(db_session):
    """Messages where sender_id matches page_id should be ignored."""
    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "page_123"},
                        "recipient": {"id": "user_456"},
                        "timestamp": 1704067200000,
                        "message": {"mid": "m_self", "text": "Self reply"},
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_facebook_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_messenger_webhook(db_session, payload)

    assert results == []
    mock_receive.assert_not_called()


def test_process_instagram_webhook_skips_self_sender(db_session):
    """Instagram messages from the business account should be ignored."""
    payload = MetaWebhookPayload(
        object="instagram",
        entry=[
            {
                "id": "ig_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "ig_123"},
                        "recipient": {"id": "user_456"},
                        "timestamp": 1704067200000,
                        "message": {"mid": "m_self", "text": "Self DM"},
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_instagram_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_instagram_webhook(db_session, payload)

    assert results == []
    mock_receive.assert_not_called()


def test_process_webhook_skips_self_facebook_comment(db_session):
    """Comments authored by the page should be ignored."""
    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "post_id": "post_1",
                            "comment_id": "c1",
                            "sender_id": "page_123",
                            "message": "Self comment",
                            "created_time": "2024-01-01T00:00:00+0000",
                        },
                    }
                ],
            }
        ],
    )

    with patch.object(meta_webhooks.comments_service, "upsert_social_comment") as mock_upsert:
        results = meta_webhooks.process_messenger_webhook(db_session, payload)

    assert results == []
    mock_upsert.assert_not_called()


def test_process_webhook_skips_self_instagram_comment(db_session):
    """Comments authored by the Instagram business account should be ignored."""
    payload = MetaWebhookPayload(
        object="instagram",
        entry=[
            {
                "id": "ig_123",
                "time": 1704067200000,
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "c1",
                            "media_id": "m1",
                            "from": {"id": "ig_123", "username": "self"},
                            "text": "Self comment",
                            "timestamp": "2024-01-01T00:00:00+0000",
                        },
                    }
                ],
            }
        ],
    )

    with patch.object(meta_webhooks.comments_service, "upsert_social_comment") as mock_upsert:
        results = meta_webhooks.process_instagram_webhook(db_session, payload)

    assert results == []
    mock_upsert.assert_not_called()


def test_process_webhook_read_receipt(db_session):
    """Test that read receipts are handled gracefully."""
    payload = MetaWebhookPayload(
        object="page",
        entry=[
            {
                "id": "page_123",
                "time": 1704067200000,
                "messaging": [
                    {
                        "sender": {"id": "user_456"},
                        "recipient": {"id": "page_123"},
                        "timestamp": 1704067200000,
                        "read": {"watermark": 1704067200000},
                    }
                ],
            }
        ],
    )

    with patch.object(
        meta_webhooks, "receive_facebook_message", return_value=MagicMock()
    ) as mock_receive:
        results = meta_webhooks.process_messenger_webhook(db_session, payload)

        # Read receipts should not create messages
        mock_receive.assert_not_called()
