from types import SimpleNamespace

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OntAssignment, OntUnit
from app.models.subscriber import Subscriber
from app.services.customer_portal_flow_services import (
    get_service_detail,
    reboot_customer_subscription_ont,
    update_customer_subscription_wifi,
)


def _active_subscription_with_ont(db_session):
    subscriber = Subscriber(
        first_name="Portal",
        last_name="User",
        email="portal-device@example.com",
    )
    offer = CatalogOffer(
        name="Portal Fiber",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add_all([subscriber, offer])
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
    )
    ont = OntUnit(serial_number="PORTAL-ONT-001", is_active=True)
    db_session.add_all([subscription, ont])
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            subscriber_id=subscriber.id,
            active=True,
            wifi_ssid="ExistingSSID",
        )
    )
    db_session.commit()
    return subscriber, subscription, ont


def test_service_detail_exposes_customer_reboot_when_ont_is_linked(db_session):
    subscriber, subscription, ont = _active_subscription_with_ont(db_session)

    detail = get_service_detail(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
    )

    assert detail is not None
    assert detail["can_reboot_ont"] is True
    assert detail["can_update_wifi"] is True
    assert detail["customer_wifi_ssid"] == "ExistingSSID"
    assert detail["customer_ont"].id == ont.id


def test_customer_reboot_delegates_to_tracked_ont_action(db_session, monkeypatch):
    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    calls = []

    def fake_execute_reboot(db, ont_id, *, initiated_by=None, request=None):
        calls.append((ont_id, initiated_by, request))
        return SimpleNamespace(success=True, message="TR-069 reboot sent")

    monkeypatch.setattr(
        "app.services.customer_portal_flow_services.ont_device_actions.execute_reboot",
        fake_execute_reboot,
    )

    ok, message = reboot_customer_subscription_ont(
        db_session,
        {"account_id": str(subscriber.id), "id": "customer-user-1"},
        str(subscription.id),
    )

    assert ok is True
    assert message == "TR-069 reboot sent"
    assert calls == [(str(ont.id), "customer:customer-user-1", None)]


def test_customer_wifi_update_delegates_to_existing_wifi_action(
    db_session, monkeypatch
):
    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    calls = []

    def fake_set_wifi_config(
        db, ont_id, *, ssid=None, password=None, request=None, **_
    ):
        calls.append((ont_id, ssid, password, request))
        return SimpleNamespace(success=True, message="WiFi updated")

    monkeypatch.setattr(
        "app.services.customer_portal_flow_services.ont_config_setters.set_wifi_config",
        fake_set_wifi_config,
    )

    ok, message = update_customer_subscription_wifi(
        db_session,
        {"account_id": str(subscriber.id), "id": "customer-user-1"},
        str(subscription.id),
        ssid="NewSSID",
        password="Secret123",
        password_confirm="Secret123",
    )

    assert ok is True
    assert message == "WiFi updated"
    assert calls == [(str(ont.id), "NewSSID", "Secret123", None)]


def test_customer_wifi_update_rejects_password_mismatch(db_session):
    subscriber, subscription, _ont = _active_subscription_with_ont(db_session)

    ok, message = update_customer_subscription_wifi(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
        ssid="NewSSID",
        password="Secret123",
        password_confirm="Different123",
    )

    assert ok is False
    assert message == "WiFi passwords do not match"


def test_customer_reboot_blocked_during_cooldown(db_session, monkeypatch):
    """A recent reboot operation on the same ONT blocks another customer
    reboot until the cooldown elapses (default 300s)."""
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    db_session.add(
        NetworkOperation(
            operation_type=NetworkOperationType.ont_reboot,
            target_type=NetworkOperationTargetType.ont,
            target_id=ont.id,
            status=NetworkOperationStatus.succeeded,
        )
    )
    db_session.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.customer_portal_flow_services.ont_device_actions.execute_reboot",
        lambda *a, **k: (
            calls.append(1) or SimpleNamespace(success=True, message="sent")
        ),
    )

    ok, message = reboot_customer_subscription_ont(
        db_session,
        {"account_id": str(subscriber.id), "id": "customer-user-1"},
        str(subscription.id),
    )

    assert ok is False
    assert "wait" in message.lower()
    assert calls == []


def test_customer_reboot_allowed_after_cooldown(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    op = NetworkOperation(
        operation_type=NetworkOperationType.ont_reboot,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.succeeded,
    )
    db_session.add(op)
    db_session.commit()
    # Age the operation past the default 300s cooldown.
    op.created_at = datetime.now(UTC) - timedelta(seconds=301)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.customer_portal_flow_services.ont_device_actions.execute_reboot",
        lambda *a, **k: SimpleNamespace(success=True, message="sent"),
    )

    ok, _ = reboot_customer_subscription_ont(
        db_session,
        {"account_id": str(subscriber.id), "id": "customer-user-1"},
        str(subscription.id),
    )
    assert ok is True


def test_failed_reboot_does_not_arm_cooldown(db_session, monkeypatch):
    """A reboot that errored never disrupted the device — it must not lock
    the customer out with 'restarted recently'."""
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )

    subscriber, subscription, ont = _active_subscription_with_ont(db_session)
    db_session.add(
        NetworkOperation(
            operation_type=NetworkOperationType.ont_reboot,
            target_type=NetworkOperationTargetType.ont,
            target_id=ont.id,
            status=NetworkOperationStatus.failed,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.customer_portal_flow_services.ont_device_actions.execute_reboot",
        lambda *a, **k: SimpleNamespace(success=True, message="sent"),
    )

    ok, _ = reboot_customer_subscription_ont(
        db_session,
        {"account_id": str(subscriber.id), "id": "customer-user-1"},
        str(subscription.id),
    )
    assert ok is True
