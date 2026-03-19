from unittest.mock import patch

import pytest
from starlette.datastructures import FormData

from app.models.catalog import AccessCredential
from app.models.network import IPAssignment
from app.models.subscriber import ChannelType, SubscriberChannel
from app.schemas.catalog import SubscriptionCreate
from app.services import auth_flow as auth_flow_service
from app.services import catalog as catalog_service
from app.services import web_catalog_subscriptions as web_catalog_subscriptions_service
from app.services import web_network_ip as web_network_ip_service


def test_apply_generated_service_credentials_keeps_custom_password_on_edit(
    db_session,
    subscriber,
):
    subscriber.subscriber_number = "SUB-004167"
    db_session.commit()

    form_data = {
        "id": "existing-subscription-id",
        "subscriber_id": str(subscriber.id),
        "account_id": str(subscriber.id),
        "service_password": "CustomPass123",
    }

    web_catalog_subscriptions_service.apply_generated_service_credentials(
        db_session, form_data
    )

    assert form_data["login"] == "10004167"
    assert form_data["service_password"] == "CustomPass123"


def test_upsert_access_credential_does_not_clear_password_when_blank_on_edit(
    db_session,
    subscriber,
    catalog_offer,
):
    subscriber.subscriber_number = "SUB-004167"
    db_session.commit()

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="InitialPass123",
        radius_profile_id=None,
    )

    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .one()
    )
    original_hash = credential.secret_hash
    assert auth_flow_service.verify_password("InitialPass123", original_hash)

    web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        None,
        None,
    )

    db_session.refresh(credential)
    assert credential.username == "10004167"
    assert credential.secret_hash == original_hash
    assert auth_flow_service.verify_password("InitialPass123", credential.secret_hash)


def test_upsert_access_credential_stores_service_password_in_reversible_format(
    db_session,
    subscriber,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="InitialPass123",
        radius_profile_id=None,
    )

    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .one()
    )

    assert credential.secret_hash.startswith(("plain:", "enc:"))
    assert auth_flow_service.verify_password("InitialPass123", credential.secret_hash)


def test_subscription_form_context_exposes_current_service_password(
    db_session,
    subscriber,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="VisiblePass123",
        radius_profile_id=None,
    )

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session,
        {
            "id": "sub-1",
            "subscriber_id": str(subscriber.id),
            "account_id": str(subscriber.id),
            "login": "10004167",
            "service_password": "",
        },
    )

    assert context["current_service_login"] == "10004167"
    assert context["current_service_password"] == "VisiblePass123"


def test_send_subscription_credentials_uses_email_and_sms_targets(
    db_session,
    subscriber,
    catalog_offer,
    monkeypatch,
):
    subscriber.email = "primary@example.com"
    subscriber.phone = "+2348000000001"
    db_session.add(
        SubscriberChannel(
            subscriber_id=subscriber.id,
            channel_type=ChannelType.email,
            address="backup@example.com",
        )
    )
    db_session.add(
        SubscriberChannel(
            subscriber_id=subscriber.id,
            channel_type=ChannelType.sms,
            address="+2348000000002",
        )
    )
    db_session.commit()

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="10004167",
        plain_password="SendablePass123",
        radius_profile_id=None,
    )

    sent_emails = []
    sent_sms = []

    def _fake_send_email(**kwargs):
        sent_emails.append(kwargs["to_email"])
        return True

    def _fake_send_sms(_db, phone, body, track=True):
        sent_sms.append((phone, body, track))
        return True

    monkeypatch.setattr(
        web_catalog_subscriptions_service.email_service,
        "send_email",
        _fake_send_email,
    )
    monkeypatch.setattr(
        web_catalog_subscriptions_service.sms_service,
        "send_sms",
        _fake_send_sms,
    )

    result = web_catalog_subscriptions_service.send_subscription_credentials(
        db_session,
        subscription_id=str(subscription.id),
    )

    assert result["email_sent"] == 2
    assert result["sms_sent"] == 2
    assert sent_emails == ["primary@example.com", "backup@example.com"]
    assert [row[0] for row in sent_sms] == ["+2348000000001", "+2348000000002"]


def test_create_subscription_with_audit_uses_requested_free_ipv4(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Subscription IPv4 Pool",
            "ip_version": "ipv4",
            "cidr": "10.80.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    block, block_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.80.0.0/29",
            "is_active": True,
        },
    )
    assert block_error is None
    assert block is not None

    payload = {
        "account_id": subscriber.id,
        "offer_id": catalog_offer.id,
    }
    form = FormData(
        [
            ("ipv4_method", "permanent_static"),
            ("ipv4_block_ids", str(block.id)),
            ("ipv4_addresses", "10.80.0.5"),
        ]
    )

    created = web_catalog_subscriptions_service.create_subscription_with_audit(
        db_session,
        payload,
        form,
        None,
        None,
    )

    db_session.refresh(created)
    assert created.ipv4_address == "10.80.0.5"

    assignment = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscription_id == created.id)
        .one()
    )
    assert assignment.ipv4_address is not None
    assert assignment.ipv4_address.address == "10.80.0.5"


@patch("app.services.radius.reconcile_subscription_connectivity")
def test_create_subscription_with_audit_reconciles_active_subscription_after_credential_sync(
    reconcile_subscription_connectivity,
    db_session,
    subscriber,
    catalog_offer,
):
    payload = {
        "account_id": subscriber.id,
        "offer_id": catalog_offer.id,
        "status": "active",
    }
    form = FormData(
        [
            ("service_password", "password"),
        ]
    )

    created = web_catalog_subscriptions_service.create_subscription_with_audit(
        db_session,
        payload,
        form,
        None,
        None,
    )

    assert reconcile_subscription_connectivity.call_count >= 1
    reconcile_subscription_connectivity.assert_any_call(
        db_session,
        str(created.id),
    )


def test_ensure_ipv4_blocks_allocatable_rejects_duplicate_manual_ipv4_selection(
    db_session,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Duplicate IPv4 Pool",
            "ip_version": "ipv4",
            "cidr": "10.81.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    first_block, first_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.81.0.0/30",
            "is_active": True,
        },
    )
    second_block, second_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.81.0.4/30",
            "is_active": True,
        },
    )
    assert first_error is None
    assert second_error is None
    assert first_block is not None
    assert second_block is not None

    with pytest.raises(ValueError, match="selected more than once"):
        web_catalog_subscriptions_service.ensure_ipv4_blocks_allocatable(
            db_session,
            [str(first_block.id), str(second_block.id)],
            ["10.81.0.5", "10.81.0.5"],
        )
