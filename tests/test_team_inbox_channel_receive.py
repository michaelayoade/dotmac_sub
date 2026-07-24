from __future__ import annotations

from datetime import UTC, datetime

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


# --- indexed multi-result matching -----------------------------------------
#
# The first attempt at this delegated to the single-answer customer-identity
# resolver and lost the matched/suppressed subscriber-id lists, regressing the
# ambiguous and suppressed cases. These pin the multi-result contract.


def _index(db, *, identity_type, normalized, subscriber):
    from app.models.customer_identity import CustomerIdentityIndex

    db.add(
        CustomerIdentityIndex(
            identity_type=identity_type,
            normalized_value=normalized,
            subscriber_id=subscriber.id,
            source_table="subscribers",
            source_field="phone",
        )
    )
    db.commit()


def test_indexed_match_resolves_single_subscriber(db_session):
    sub = _subscriber(db_session, phone="+2348035551000", email="idx1@example.com")
    _index(
        db_session, identity_type="phone", normalized="+2348035551000", subscriber=sub
    )

    resolution = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="08035551000",
    )
    assert resolution.status == "linked_subscriber"
    assert resolution.subscriber_id == sub.id


def test_indexed_ambiguous_keeps_every_matched_id(db_session):
    """The regression that forced the revert: an ambiguous contact must record
    ALL matches, never silently resolve to one of several people."""
    a = _subscriber(db_session, phone="+2348035551001", email="a2@example.com")
    b = _subscriber(db_session, phone="+2348035551001", email="b2@example.com")
    for sub in (a, b):
        _index(
            db_session,
            identity_type="phone",
            normalized="+2348035551001",
            subscriber=sub,
        )

    resolution = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="08035551001",
    )
    assert resolution.status == "ambiguous"
    assert resolution.subscriber_id is None
    assert len(resolution.matched_subscriber_ids) == 2
    assert set(resolution.matched_subscriber_ids) == {str(a.id), str(b.id)}


def test_indexed_inactive_matches_are_suppressed_with_their_ids(db_session):
    """The second regression: multiple inactive matches must surface as
    suppressed_inactive with their ids, not vanish into unmatched."""
    disabled = _subscriber(
        db_session,
        phone="+2348035551002",
        email="d2@example.com",
        status=SubscriberStatus.disabled,
    )
    canceled = _subscriber(
        db_session,
        phone="+2348035551002",
        email="c2@example.com",
        status=SubscriberStatus.canceled,
    )
    for sub in (disabled, canceled):
        _index(
            db_session,
            identity_type="phone",
            normalized="+2348035551002",
            subscriber=sub,
        )

    resolution = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="08035551002",
    )
    assert resolution.status == "suppressed_inactive"
    assert resolution.subscriber_id is None
    assert len(resolution.suppressed_subscriber_ids) == 2


def test_index_covers_contacts_the_old_scan_missed(db_session):
    """The coverage gain: the index spans SubscriberContact/SubscriberChannel,
    so a number on a contact row resolves even though Subscriber.phone differs."""
    sub = _subscriber(db_session, phone="+2340000000000", email="cov@example.com")
    _index(
        db_session,
        identity_type="phone",
        normalized="+2348035551003",
        subscriber=sub,
    )

    resolution = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="08035551003",
    )
    assert resolution.status == "linked_subscriber"
    assert resolution.subscriber_id == sub.id


def test_empty_index_falls_back_to_the_scan(db_session):
    """An un-backfilled deployment must not silently resolve nobody."""
    sub = _subscriber(db_session, phone="+2348035551004", email="fb@example.com")

    resolution = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="08035551004",
    )
    assert resolution.status == "linked_subscriber"
    assert resolution.subscriber_id == sub.id


def test_populated_index_with_no_match_does_not_rescan(db_session):
    """A populated index answering 'no match' is authoritative — otherwise
    every unknown number would trigger a full-table scan."""
    other = _subscriber(db_session, phone="+2348035551005", email="o@example.com")
    _index(
        db_session, identity_type="phone", normalized="+2348035551005", subscriber=other
    )
    _subscriber(db_session, phone="+2348035559999", email="unindexed@example.com")

    resolution = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="08035559999",
    )
    assert resolution.status == "unmatched"


def test_opaque_channel_still_never_auto_matches(db_session):
    sub = _subscriber(db_session, phone="+2348035551006", email="ig2@example.com")
    _index(
        db_session, identity_type="phone", normalized="17841400000123", subscriber=sub
    )

    resolution = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type=InboxChannelType.instagram_dm.value,
        contact_address="17841400000123",
    )
    assert resolution.status == "unmatched"
