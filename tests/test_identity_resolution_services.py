"""Tests for identity resolution and normalization across channels."""

from datetime import datetime, timezone

from app.models.person import Person, PersonChannel, ChannelType as PersonChannelType
from app.models.subscriber import Subscriber, SubscriberAccount
from app.models.crm.enums import ChannelType
from app.services.crm import contact as contact_service
from app.services.crm import inbox as inbox_service
from app.services import meta_webhooks


def test_email_normalization_prevents_duplicate_channels(db_session):
    person1, channel1 = contact_service.get_or_create_contact_by_channel(
        db_session,
        ChannelType.email,
        "User@Example.com",
    )
    person2, channel2 = contact_service.get_or_create_contact_by_channel(
        db_session,
        ChannelType.email,
        "user@example.com",
    )

    assert person1.id == person2.id
    assert channel1.id == channel2.id
    assert channel1.address == "user@example.com"

    count = (
        db_session.query(PersonChannel)
        .filter(PersonChannel.person_id == person1.id)
        .filter(PersonChannel.channel_type == PersonChannelType.email)
        .count()
    )
    assert count == 1


def test_phone_normalization_prevents_duplicate_channels(db_session):
    channel_type = ChannelType.whatsapp
    person1, channel1 = contact_service.get_or_create_contact_by_channel(
        db_session,
        channel_type,
        "+1 (555) 123-4567",
    )
    person2, channel2 = contact_service.get_or_create_contact_by_channel(
        db_session,
        channel_type,
        "15551234567",
    )

    assert person1.id == person2.id
    assert channel1.id == channel2.id
    assert channel1.address == "15551234567"

    count = (
        db_session.query(PersonChannel)
        .filter(PersonChannel.person_id == person1.id)
        .filter(PersonChannel.channel_type == PersonChannelType.whatsapp)
        .count()
    )
    assert count == 1


def test_phone_channels_link_across_whatsapp_formats(db_session):
    person1, channel1 = contact_service.get_or_create_contact_by_channel(
        db_session,
        ChannelType.whatsapp,
        "+1 (555) 888-0000",
    )
    person2, channel2 = contact_service.get_or_create_contact_by_channel(
        db_session,
        ChannelType.whatsapp,
        "15558880000",
    )

    assert person1.id == person2.id
    assert channel1.channel_type == PersonChannelType.whatsapp
    assert channel2.channel_type == PersonChannelType.whatsapp
    assert channel2.address == "15558880000"


def test_email_inbound_updates_placeholder_email(db_session):
    person = Person(
        first_name="Test",
        last_name="Placeholder",
        email="whatsapp-15551234567@example.invalid",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    subscriber = Subscriber(person_id=person.id)
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)

    account = SubscriberAccount(subscriber_id=subscriber.id)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    email_address = "Real.Email@Example.com"
    resolved_person, channel = inbox_service._resolve_person_for_inbound(
        db_session,
        ChannelType.email,
        email_address,
        display_name=None,
        account=account,
    )

    assert resolved_person.id == person.id
    assert resolved_person.email == "real.email@example.com"
    assert channel.address == "real.email@example.com"


def test_whatsapp_inbound_links_to_existing_phone(db_session):
    person = Person(
        first_name="WhatsApp",
        last_name="Link",
        email="sms.link@example.com",
        phone="15557778888",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    resolved_person, channel = inbox_service._resolve_person_for_inbound(
        db_session,
        ChannelType.whatsapp,
        "+1 (555) 777-8888",
        display_name=None,
        account=None,
    )

    assert resolved_person.id == person.id
    assert channel.channel_type == PersonChannelType.whatsapp
    assert channel.address == "15557778888"


def test_meta_webhook_links_by_email_metadata(db_session):
    person = Person(
        first_name="Meta",
        last_name="User",
        display_name="Meta User",
        email="meta.user@example.com",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    email_channel = PersonChannel(
        person_id=person.id,
        channel_type=PersonChannelType.email,
        address="meta.user@example.com",
        is_primary=True,
    )
    db_session.add(email_channel)
    db_session.commit()

    resolved_person, channel = meta_webhooks._resolve_meta_person_and_channel(
        db_session,
        ChannelType.facebook_messenger,
        "page_scoped_sender_id",
        "Meta User",
        metadata={"email": "Meta.User@Example.com"},
    )

    assert resolved_person.id == person.id
    assert channel.channel_type == PersonChannelType.facebook_messenger
    assert channel.address == "page_scoped_sender_id"


def test_meta_webhook_links_by_phone_metadata(db_session):
    person = Person(
        first_name="Meta",
        last_name="Phone",
        display_name="Meta Phone",
        email="meta.phone@example.com",
        phone="15551234567",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    phone_channel = PersonChannel(
        person_id=person.id,
        channel_type=PersonChannelType.phone,
        address="15551234567",
        is_primary=True,
    )
    db_session.add(phone_channel)
    db_session.commit()

    resolved_person, channel = meta_webhooks._resolve_meta_person_and_channel(
        db_session,
        ChannelType.instagram_dm,
        "ig_scoped_sender_id",
        "Meta Phone",
        metadata={"phone": "+1 (555) 123-4567"},
    )

    assert resolved_person.id == person.id
    assert channel.channel_type == PersonChannelType.instagram_dm
    assert channel.address == "ig_scoped_sender_id"


def test_meta_webhook_links_by_phone_metadata_whatsapp_channel(db_session):
    person = Person(
        first_name="Meta",
        last_name="WhatsApp",
        display_name="Meta WhatsApp",
        email="meta.whatsapp@example.com",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    whatsapp_channel = PersonChannel(
        person_id=person.id,
        channel_type=PersonChannelType.whatsapp,
        address="15559990000",
        is_primary=True,
    )
    db_session.add(whatsapp_channel)
    db_session.commit()

    resolved_person, channel = meta_webhooks._resolve_meta_person_and_channel(
        db_session,
        ChannelType.facebook_messenger,
        "fb_scoped_sender_id",
        "Meta WhatsApp",
        metadata={"phone": "+1 (555) 999-0000"},
    )

    assert resolved_person.id == person.id
    assert channel.channel_type == PersonChannelType.facebook_messenger
