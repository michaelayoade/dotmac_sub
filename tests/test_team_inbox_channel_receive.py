from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import InboxChannelType, InboxConversation, InboxMessage
from app.services import team_inbox_channel_receive


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _reseller(
    db_session,
    *,
    name: str = "Partner",
    phone: str | None = None,
) -> Reseller:
    reseller = Reseller(
        name=name,
        code=name.lower().replace(" ", "-"),
        contact_phone=phone,
        is_active=True,
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def _subscriber(
    db_session,
    *,
    phone: str,
    email: str,
    reseller: Reseller | None = None,
    status: SubscriberStatus = SubscriberStatus.active,
    is_active: bool = True,
) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email=email,
        phone=phone,
        status=status,
        is_active=is_active,
        reseller_id=reseller.id if reseller else None,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_receive_whatsapp_links_single_active_subscriber_and_reseller(db_session):
    team = _team(db_session)
    reseller = _reseller(db_session, name="North Partner")
    subscriber = _subscriber(
        db_session,
        phone="0803 555 0114",
        email="ada@example.com",
        reseller=reseller,
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="whatsapp:+2348035550114",
            body="My service is down",
            external_message_id="wamid-1",
            fallback_service_team_id=team.id,
            received_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    resolution = conversation.metadata_["contact_resolution"]
    assert result.kind == "received"
    assert result.subscriber_id == str(subscriber.id)
    assert result.reseller_id == str(reseller.id)
    assert result.resolution_status == "linked_subscriber"
    assert conversation.subscriber_id == subscriber.id
    assert conversation.primary_service_team_id == team.id
    assert conversation.contact_address == "+2348035550114"
    assert message.from_address == "+2348035550114"
    assert message.metadata_["contact_resolution"]["subscriber_id"] == str(
        subscriber.id
    )
    assert resolution["reseller_id"] == str(reseller.id)


def test_receive_whatsapp_records_ambiguous_shared_phone_without_guessing(db_session):
    _subscriber(
        db_session,
        phone="0803 555 0114",
        email="ada@example.com",
    )
    _subscriber(
        db_session,
        phone="+2348035550114",
        email="shared@example.com",
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="08035550114",
            body="Who owns this?",
            external_message_id="wamid-ambiguous",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    resolution = conversation.metadata_["contact_resolution"]
    assert result.subscriber_id is None
    assert result.resolution_status == "ambiguous"
    assert conversation.subscriber_id is None
    assert len(resolution["matched_subscriber_ids"]) == 2


def test_receive_whatsapp_suppresses_disabled_or_canceled_matches(db_session):
    disabled = _subscriber(
        db_session,
        phone="0803 555 0114",
        email="disabled@example.com",
        status=SubscriberStatus.disabled,
    )
    canceled = _subscriber(
        db_session,
        phone="+2348035550114",
        email="canceled@example.com",
        status=SubscriberStatus.canceled,
    )
    inactive = _subscriber(
        db_session,
        phone="+2348035550114",
        email="inactive@example.com",
        is_active=False,
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="08035550114",
            body="Please reactivate me",
            external_message_id="wamid-suppressed",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    resolution = conversation.metadata_["contact_resolution"]
    assert result.subscriber_id is None
    assert conversation.subscriber_id is None
    assert set(resolution["suppressed_subscriber_ids"]) == {
        str(disabled.id),
        str(canceled.id),
        str(inactive.id),
    }


def test_receive_whatsapp_links_reseller_contact_without_subscriber(db_session):
    reseller = _reseller(db_session, name="VIP Reseller", phone="0808 111 2222")
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="08081112222",
            body="One of my customers is down",
            external_message_id="wamid-reseller",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert result.subscriber_id is None
    assert result.reseller_id == str(reseller.id)
    assert result.resolution_status == "linked_reseller"
    assert conversation.metadata_["contact_resolution"]["reseller_id"] == str(
        reseller.id
    )


def test_receive_whatsapp_webhook_normalizes_and_deduplicates(db_session):
    first = team_inbox_channel_receive.receive_whatsapp_webhook(
        db_session,
        provider="meta_cloud_api",
        payload={
            "message": {
                "from": "2348012345678",
                "text": "Hello",
                "id": "wamid-1",
            },
        },
    )
    second = team_inbox_channel_receive.receive_whatsapp_webhook(
        db_session,
        provider="meta_cloud_api",
        payload={
            "message": {
                "from": "2348012345678",
                "text": "Hello again",
                "id": "wamid-1",
            },
        },
    )
    db_session.commit()

    message = db_session.get(InboxMessage, first.message_id)
    assert first.kind == "received"
    assert second.kind == "duplicate"
    assert second.conversation_id == first.conversation_id
    assert message.channel_type == InboxChannelType.whatsapp.value
    assert message.from_address == "+2348012345678"
    assert message.body == "Hello"


def test_committed_whatsapp_batch_calls_shared_customer_ai_intake(
    db_session, monkeypatch
):
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        team_inbox_channel_receive,
        "_run_customer_ai_intake_after_commit",
        lambda _db, *, conversation_id, message_id: calls.append(
            (conversation_id, message_id)
        ),
    )

    results, statuses = (
        team_inbox_channel_receive.receive_whatsapp_webhook_batch_committed(
            db_session,
            provider="meta_cloud_api",
            payloads=[
                {
                    "message": {
                        "from": "2348012345678",
                        "text": "I need a new connection",
                        "id": "wamid-shared-ai-hook",
                    },
                    "observed_at": datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
                    "metadata": {"phone_number_id": "phone-1"},
                }
            ],
        )
    )

    assert statuses == []
    assert len(results) == 1
    assert len(calls) == 1
    assert str(calls[0][0]) == results[0]["conversation_id"]
    assert str(calls[0][1]) == results[0]["message_id"]


@pytest.mark.parametrize(
    "channel_type",
    [
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
    ],
)
def test_committed_meta_social_batch_calls_same_shared_customer_ai_intake(
    db_session, monkeypatch, channel_type
):
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        team_inbox_channel_receive,
        "_run_customer_ai_intake_after_commit",
        lambda _db, *, conversation_id, message_id: calls.append(
            (conversation_id, message_id)
        ),
    )

    results = team_inbox_channel_receive.receive_inbound_channel_batch_committed(
        db_session,
        [
            team_inbox_channel_receive.InboundChannelPayload(
                channel_type=channel_type,
                contact_address=f"sender-{channel_type}",
                body="I need a new connection",
                external_message_id=f"{channel_type}-shared-ai-hook",
                received_at=datetime(2026, 8, 4, 12, 1, tzinfo=UTC),
                metadata={
                    "provider": "meta_social",
                    "page_or_account_id": f"account-{channel_type}",
                },
            )
        ],
    )

    assert len(results) == 1
    assert len(calls) == 1
    assert str(calls[0][0]) == results[0]["conversation_id"]
    assert str(calls[0][1]) == results[0]["message_id"]
