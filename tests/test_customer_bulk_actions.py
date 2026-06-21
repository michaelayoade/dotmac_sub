from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
)
from app.models.subscriber import Subscriber, UserType
from app.services import web_customer_actions


def test_bulk_update_customers_from_filtered_scope_updates_matching_customers(
    db_session,
):
    matched = Subscriber(
        first_name="Ada",
        last_name="Scope",
        email="ada-scope@example.com",
        user_type=UserType.customer,
        is_active=True,
        billing_enabled=True,
    )
    other = Subscriber(
        first_name="Ben",
        last_name="Other",
        email="ben-other@example.com",
        user_type=UserType.customer,
        is_active=True,
        billing_enabled=True,
    )
    db_session.add_all([matched, other])
    db_session.commit()

    result = web_customer_actions.bulk_update_customers_from_payload(
        db_session,
        {
            "filters": {"search": "ada-scope@example.com"},
            "updates": {
                "account_state": "inactive",
                "billing_enabled": False,
                "payment_method": "bank_transfer",
            },
        },
    )

    db_session.refresh(matched)
    db_session.refresh(other)

    assert result["scope"] == "filtered"
    assert result["updated_count"] == 1
    assert matched.is_active is False
    assert matched.billing_enabled is False
    assert matched.payment_method == "bank_transfer"
    assert other.is_active is True
    assert other.billing_enabled is True


def test_queue_bulk_message_from_selected_scope_renders_template_and_skips_missing_recipient(
    db_session,
):
    reachable = Subscriber(
        first_name="Rita",
        last_name="Reachable",
        email="rita@example.com",
        phone="+2348011111111",
        account_number="AC-1001",
        user_type=UserType.customer,
        is_active=True,
    )
    missing_phone = Subscriber(
        first_name="Sam",
        last_name="NoPhone",
        email="sam@example.com",
        phone=None,
        account_number="AC-1002",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Outage SMS",
        code="outage_sms",
        channel=NotificationChannel.sms,
        body="Hello {customer_name} on {account_number}",
        is_active=True,
    )
    db_session.add_all([reachable, missing_phone, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {
            "customer_ids": [
                {"id": str(reachable.id), "type": "person"},
                {"id": str(missing_phone.id), "type": "person"},
            ],
            "channel": "sms",
            "template_id": str(template.id),
        },
    )

    assert result["scope"] == "selected"
    assert result["matched_count"] == 2
    assert result["created_count"] == 1
    assert result["queued_count"] == 1
    assert len(result["skipped"]) == 1

    notification = db_session.get(Notification, result["notification_ids"][0])
    assert notification is not None
    assert notification.recipient == "+2348011111111"
    assert notification.body == "Hello Rita Reachable on AC-1001"
