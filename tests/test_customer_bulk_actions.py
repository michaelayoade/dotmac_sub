from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
)
from app.models.subscriber import Subscriber, UserType
from app.services import web_customer_actions

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_customer_whatsapp_template_lookup_fetch_handles_non_json_errors():
    template = (REPO_ROOT / "templates/admin/customers/index.html").read_text()

    assert "'Accept': 'application/json'" in template
    assert "const raw = await response.text();" in template
    assert "JSON.parse(raw)" in template
    assert (
        "Could not load WhatsApp template details (HTTP ${response.status})."
        in template
    )


def test_customer_send_email_action_opens_template_modal_for_email_channel():
    table_template = (REPO_ROOT / "templates/admin/customers/_table.html").read_text()
    page_template = (REPO_ROOT / "templates/admin/customers/index.html").read_text()
    detail_template = (REPO_ROOT / "templates/admin/customers/detail.html").read_text()

    assert "mailto:" not in table_template
    assert "customer-send-message" in table_template
    assert "channel: 'email'" in table_template
    assert "const { id, type, channel } = e.detail;" in page_template
    assert "this.resetSendMessageForm(channel);" in page_template
    assert "@click.prevent.stop=\"openSendMessageModal('email')\"" in detail_template
    assert "detailUrl" in detail_template
    assert "closeSendMessageModal" in detail_template
    assert "mailto:" not in detail_template


def test_customer_bulk_actions_sync_selection_from_checked_rows_before_submit():
    page_template = (REPO_ROOT / "templates/admin/customers/index.html").read_text()

    assert "syncSelectionFromDom()" in page_template
    assert "applySelectionToDom()" in page_template
    assert (
        "document.querySelectorAll('#customers-table [data-customer-checkbox]:checked')"
        in page_template
    )
    assert (
        "document.querySelectorAll('#customers-table [data-customer-checkbox]')"
        in page_template
    )
    assert "this.syncSelectionFromDom();" in page_template
    assert "this.applySelectionToDom();" in page_template
    assert (
        "this.clearSelection();"
        not in page_template.split("htmx:afterSwap", 1)[1].split("});", 1)[0]
    )
    assert (
        "this.selectedIds.push({ id: customer.id, type: customer.type });"
        in page_template
    )
    assert (
        "this.selectedIds.filter((item) => !visibleIds.has(item.id))" in page_template
    )
    assert "Matched ${matched} customer(s)." in page_template
    assert "skipped due to missing contact details" in page_template
    assert "suppressed by customer preferences" in page_template


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
            "confirmed": True,
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


def test_queue_bulk_email_backfills_common_template_aliases(db_session):
    customer = Subscriber(
        first_name="Chidinma",
        last_name="Onyemachi",
        email="chidinma@example.com",
        account_number="ACC-005069",
        user_type=UserType.customer,
        is_active=True,
    )
    offer = CatalogOffer(
        name="Dotmac Fiber 50Mbps",
        code="fiber_50",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    subscription = Subscription(
        subscriber=customer,
        offer=offer,
        status=SubscriptionStatus.active,
    )
    template = NotificationTemplate(
        name="Manual Email",
        code="manual_email",
        channel=NotificationChannel.email,
        subject="Hello {subscriber_name}",
        body=(
            "Dear {subscriber_name}, "
            "your {{offer_name}} plan on {account_number} is managed by {{company_name}}."
        ),
        is_active=True,
    )
    db_session.add_all([customer, offer, subscription, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {
            "customer_ids": [{"id": str(customer.id), "type": "person"}],
            "channel": "email",
            "template_id": str(template.id),
            "confirmed": True,
        },
    )

    notification = db_session.get(Notification, result["notification_ids"][0])
    assert notification is not None
    assert notification.subject == "Hello Chidinma Onyemachi"
    assert "Dear Chidinma Onyemachi" in notification.body
    assert "Dotmac Fiber 50Mbps" in notification.body
    assert "ACC-005069" in notification.body
    assert "{{" not in notification.body
    assert "{subscriber_name}" not in notification.body


def test_queue_bulk_email_rejects_unavailable_template_variables(db_session):
    customer = Subscriber(
        first_name="Ada",
        last_name="Blocked",
        email="ada-blocked@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Bad Manual Email",
        code="bad_manual_email",
        channel=NotificationChannel.email,
        subject="Hello {customer_name}",
        body="This should not send: {{unknown_value}}",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_customer_actions.queue_bulk_message_from_payload(
            db_session,
            {
                "customer_ids": [{"id": str(customer.id), "type": "person"}],
                "channel": "email",
                "template_id": str(template.id),
                "confirmed": True,
            },
        )

    assert exc.value.status_code == 400
    assert "{unknown_value}" in exc.value.detail


def test_queue_bulk_message_preview_does_not_create_rows(db_session):
    customer = Subscriber(
        first_name="Preview",
        last_name="Only",
        email="preview@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Preview Email",
        code="preview_email",
        channel=NotificationChannel.email,
        subject="Hello {customer_name}",
        body="Body for {account_number}",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {
            "customer_ids": [{"id": str(customer.id), "type": "person"}],
            "channel": "email",
            "template_id": str(template.id),
            "preview_only": True,
        },
    )

    assert result["preview"] is True
    assert result["created_count"] == 1
    assert result["queued_count"] == 1
    assert result["notification_ids"] == []
    assert db_session.query(Notification).count() == 0
