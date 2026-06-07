"""Customer contact creation must persist.

Regression: create_customer_contact called _create_subscriber_channels_from_rows
(which only flushes) without committing, so adding a contact from the customer
detail page returned 200 but silently persisted nothing.
"""

from app.models.subscriber import ChannelType, SubscriberChannel
from app.services import web_customer_actions as actions


def test_create_customer_contact_persists_channel(db_session, subscriber):
    actions.create_customer_contact(
        db_session,
        account_id=str(subscriber.id),
        first_name="Jane",
        last_name="Contact",
        role="primary",
        title=None,
        email="jane.contact@example.com",
        phone=None,
        is_primary="false",
    )

    # Re-query in a way that would only see committed data within this session.
    channel = (
        db_session.query(SubscriberChannel)
        .filter(SubscriberChannel.subscriber_id == subscriber.id)
        .filter(SubscriberChannel.channel_type == ChannelType.email)
        .filter(SubscriberChannel.address == "jane.contact@example.com")
        .first()
    )
    assert channel is not None
