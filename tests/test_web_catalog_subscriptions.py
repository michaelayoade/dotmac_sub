from decimal import Decimal
from datetime import UTC, datetime
import sqlite3
from unittest.mock import patch
from uuid import uuid4

import pytest
from starlette.datastructures import FormData

from app.models.billing import TaxRate
from app.models.catalog import AccessCredential, NasDevice
from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.models.event_store import EventStatus, EventStore
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.network import IPAssignment
from app.models.notification import Notification, NotificationChannel, NotificationStatus, NotificationTemplate
from app.models.radius import RadiusClient, RadiusServer, RadiusSyncJob, RadiusSyncRun, RadiusSyncStatus, RadiusUser
from app.models.subscriber import Address, ChannelType, SubscriberChannel
from app.schemas.catalog import SubscriptionCreate
from app.services import auth_flow as auth_flow_service
from app.services import catalog as catalog_service
from app.services import web_catalog_subscriptions as web_catalog_subscriptions_service
from app.services import web_network_ip as web_network_ip_service


def _billing_setting(key: str, value: str) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.billing,
        key=key,
        value_text=value,
        is_active=True,
    )


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
        [],
        [],
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


def test_subscription_detail_context_resolves_commercial_policy_from_customer_and_globals(
    db_session,
    subscription,
    subscriber,
):
    db_session.add_all(
        [
            _billing_setting("billing_day", "1"),
            _billing_setting("payment_due_days", "14"),
            _billing_setting("minimum_balance", "50.00"),
        ]
    )
    subscriber.payment_method = "bank_transfer"
    subscriber.billing_day = 6
    subscriber.grace_period_days = 3
    subscriber.min_balance = Decimal("125.00")
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    rows = {row["key"]: row for row in context["commercial_policy"]["rows"]}
    assert rows["billing_mode"]["source"] == "Subscription"
    assert rows["contract_term"]["source"] == "Subscription"
    assert rows["payment_method"]["effective"] == "bank_transfer"
    assert rows["payment_method"]["source"] == "Customer override"
    assert rows["billing_day"]["effective"] == "Day 6"
    assert rows["billing_day"]["global"] == "Day 1"
    assert rows["payment_due_days"]["effective"] == "14 day(s)"
    assert rows["payment_due_days"]["source"] == "Global default"
    assert rows["grace_period_days"]["effective"] == "3 day(s)"
    assert rows["min_balance"]["effective"] == "NGN 125.00"
    assert rows["tax_rate"]["effective"] == "Not set"


def test_subscription_form_context_prefers_service_address_tax_for_commercial_policy(
    db_session,
    subscription,
    subscriber,
):
    subscriber_tax = TaxRate(name="Customer VAT", rate=Decimal("0.075"))
    address_tax = TaxRate(name="Service VAT", rate=Decimal("0.050"))
    db_session.add_all([subscriber_tax, address_tax])
    db_session.commit()

    subscriber.tax_rate_id = subscriber_tax.id
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="123 Service Street",
        tax_rate_id=address_tax.id,
    )
    db_session.add(address)
    db_session.commit()

    subscription.service_address_id = address.id
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_form_context(
        db_session,
        web_catalog_subscriptions_service.edit_form_data(db_session, subscription),
    )

    rows = {row["key"]: row for row in context["commercial_policy"]["rows"]}
    assert rows["tax_rate"]["effective"] == "Service VAT (5.00%)"
    assert rows["tax_rate"]["source"] == "Service address"
    assert rows["tax_rate"]["override"] == "Customer VAT (7.50%)"


def test_subscription_detail_context_exposes_events_notifications_and_radius_sync(
    db_session,
    subscription,
    subscriber,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="105000111",
        plain_password="VisiblePass123",
        radius_profile_id=None,
    )
    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .one()
    )

    nas_device = NasDevice(name="Karu", nas_ip="172.16.0.1", shared_secret="plain:secret")
    radius_server = RadiusServer(name="Primary RADIUS", host="127.0.0.1")
    db_session.add_all([nas_device, radius_server])
    db_session.flush()

    subscription.provisioning_nas_device_id = nas_device.id
    db_session.add(
        RadiusUser(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            access_credential_id=credential.id,
            username=credential.username,
            secret_hash=credential.secret_hash,
            is_active=True,
            last_sync_at=datetime(2026, 3, 21, 10, 0, tzinfo=UTC),
        )
    )
    db_session.add(
        RadiusClient(
            server_id=radius_server.id,
            nas_device_id=nas_device.id,
            client_ip="172.16.0.1",
            shared_secret_hash="hashed",
            description="Karu",
            is_active=True,
        )
    )
    db_session.add(
        EventStore(
            event_id=uuid4(),
            event_type="subscription.suspended",
            payload={"from_status": "active", "to_status": "suspended", "reason": "dunning"},
            status=EventStatus.failed,
            subscription_id=subscription.id,
            account_id=subscriber.id,
            failed_handlers=[{"handler": "NotificationHandler", "error": "smtp"}],
        )
    )
    template = NotificationTemplate(
        name="Subscription Suspended",
        code="subscription_suspended",
        channel=NotificationChannel.email,
        body="Suspended",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()
    db_session.add(
        Notification(
            template_id=template.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Service suspended",
            body="Suspended",
            status=NotificationStatus.queued,
        )
    )
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    assert context["domain_events"][0]["event_type"] == "subscription.suspended"
    assert context["domain_events"][0]["failed_handler_text"] == "NotificationHandler"
    assert context["notification_evidence"]["items"][0]["template_code"] == "subscription_suspended"
    assert context["radius_sync_evidence"]["internal_user"].username == "105000111"
    assert context["radius_sync_evidence"]["nas_client_count"] == 1


def test_subscription_detail_context_exposes_latest_external_radius_sync_run(
    db_session,
    subscription,
    subscriber,
):
    radius_server = RadiusServer(name="External RADIUS", host="127.0.0.2")
    connector = ConnectorConfig(
        name="External RADIUS DB",
        connector_type=ConnectorType.custom,
        auth_type=ConnectorAuthType.none,
        is_active=True,
    )
    db_session.add_all([radius_server, connector])
    db_session.flush()

    sync_job = RadiusSyncJob(
        name="Nightly external sync",
        server_id=radius_server.id,
        connector_config_id=connector.id,
        sync_users=True,
        sync_nas_clients=True,
        is_active=True,
    )
    db_session.add(sync_job)
    db_session.flush()
    db_session.add(
        RadiusSyncRun(
            job_id=sync_job.id,
            status=RadiusSyncStatus.success,
            details={
                "external_users_synced": 12,
                "external_nas_synced": 4,
                "credentials_scanned": 12,
                "nas_devices_synced": 4,
            },
        )
    )
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    assert context["radius_sync_evidence"]["external_job_count"] == 1
    assert context["radius_sync_evidence"]["external_run"].status == RadiusSyncStatus.success
    assert context["radius_sync_evidence"]["external_users_synced"] == 12
    assert context["radius_sync_evidence"]["external_nas_synced"] == 4


def test_subscription_detail_context_reads_live_external_radius_rows(
    db_session,
    subscription,
    subscriber,
    tmp_path,
):
    web_catalog_subscriptions_service._upsert_access_credential(
        db_session,
        subscriber_id=subscriber.id,
        username="105000222",
        plain_password="SecretPass123",
        radius_profile_id=None,
    )

    db_path = tmp_path / "radius_external.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)")
    conn.execute("CREATE TABLE radreply (username TEXT, attribute TEXT, op TEXT, value TEXT)")
    conn.execute("CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INTEGER)")
    conn.execute(
        "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)",
        ("105000222", "Cleartext-Password", ":=", "SecretPass123"),
    )
    conn.execute(
        "INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)",
        ("105000222", "Framed-Pool", ":=", "pppoe-karu"),
    )
    conn.execute(
        "INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)",
        ("105000222", "fiber-1000gb", 0),
    )
    conn.commit()
    conn.close()

    radius_server = RadiusServer(name="SQLite RADIUS", host="127.0.0.3")
    connector = ConnectorConfig(
        name="SQLite external RADIUS",
        connector_type=ConnectorType.custom,
        auth_type=ConnectorAuthType.none,
        base_url=f"sqlite:///{db_path}",
        is_active=True,
    )
    db_session.add_all([radius_server, connector])
    db_session.flush()
    db_session.add(
        RadiusSyncJob(
            name="SQLite sync",
            server_id=radius_server.id,
            connector_config_id=connector.id,
            sync_users=True,
            sync_nas_clients=True,
            is_active=True,
        )
    )
    db_session.commit()

    context = web_catalog_subscriptions_service.subscription_detail_context(
        db_session,
        subscription,
    )

    source = context["external_radius_rows"][0]
    assert source["available"] is True
    assert source["radcheck"][0]["attribute"] == "Cleartext-Password"
    assert source["radcheck"][0]["value"] == "••••••••"
    assert source["radreply"][0]["value"] == "pppoe-karu"
    assert source["radusergroup"][0]["groupname"] == "fiber-1000gb"


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


def test_update_subscription_with_audit_persists_added_ipv4_assignment(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Edit Subscription IPv4 Pool",
            "ip_version": "ipv4",
            "cidr": "10.82.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    block, block_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.82.0.0/29",
            "is_active": True,
        },
    )
    assert block_error is None
    assert block is not None

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    updated = web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [str(block.id)],
        ["10.82.0.5"],
        None,
        None,
    )

    db_session.refresh(updated)
    assert updated.ipv4_address == "10.82.0.5"

    assignment = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscription_id == updated.id)
        .filter(IPAssignment.is_active.is_(True))
        .one()
    )
    assert assignment.ipv4_address is not None
    assert assignment.ipv4_address.address == "10.82.0.5"


def test_edit_form_data_includes_active_ipv4_assignments(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Existing Assignments Pool",
            "ip_version": "ipv4",
            "cidr": "10.83.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    first_block, first_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.83.0.0/30",
            "is_active": True,
        },
    )
    second_block, second_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.83.0.4/30",
            "is_active": True,
        },
    )
    assert first_error is None
    assert second_error is None
    assert first_block is not None
    assert second_block is not None

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service._allocate_ipv4_assignments_for_subscription(
        db_session,
        subscription_obj=subscription,
        block_ids=[str(first_block.id), str(second_block.id)],
        selected_ips=["10.83.0.1", "10.83.0.5"],
    )
    subscription.ipv4_address = "10.83.0.1"
    db_session.commit()
    db_session.refresh(subscription)

    form_data = web_catalog_subscriptions_service.edit_form_data(db_session, subscription)

    assert form_data["ipv4_addresses"] == ["10.83.0.1", "10.83.0.5"]
    assert form_data["ipv4_block_ids"] == [str(first_block.id), str(second_block.id)]


def test_update_subscription_with_audit_deallocates_removed_ipv4_assignments(
    db_session,
    subscriber,
    catalog_offer,
):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Deallocation Pool",
            "ip_version": "ipv4",
            "cidr": "10.84.0.0/29",
            "is_active": True,
        },
    )
    assert pool_error is None
    assert pool is not None

    first_block, first_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.84.0.0/30",
            "is_active": True,
        },
    )
    second_block, second_error = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.84.0.4/30",
            "is_active": True,
        },
    )
    assert first_error is None
    assert second_error is None
    assert first_block is not None
    assert second_block is not None

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )

    web_catalog_subscriptions_service._allocate_ipv4_assignments_for_subscription(
        db_session,
        subscription_obj=subscription,
        block_ids=[str(first_block.id), str(second_block.id)],
        selected_ips=["10.84.0.1", "10.84.0.5"],
    )
    subscription.ipv4_address = "10.84.0.1"
    db_session.commit()
    db_session.refresh(subscription)

    updated = web_catalog_subscriptions_service.update_subscription_with_audit(
        db_session,
        str(subscription.id),
        {"login": "10004167"},
        "",
        [str(first_block.id)],
        ["10.84.0.1"],
        None,
        None,
    )

    db_session.refresh(updated)
    assert updated.ipv4_address == "10.84.0.1"

    active_assignments = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscription_id == updated.id)
        .filter(IPAssignment.is_active.is_(True))
        .all()
    )
    assert len(active_assignments) == 1
    assert active_assignments[0].ipv4_address is not None
    assert active_assignments[0].ipv4_address.address == "10.84.0.1"

    inactive_assignments = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscription_id == updated.id)
        .filter(IPAssignment.is_active.is_(False))
        .all()
    )
    assert len(inactive_assignments) == 1
    assert inactive_assignments[0].ipv4_address is not None
    assert inactive_assignments[0].ipv4_address.address == "10.84.0.5"


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
