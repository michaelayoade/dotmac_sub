"""UISP desired/observed control-plane contracts."""

from __future__ import annotations

from app.models.catalog import AccessType, Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceType
from app.models.network_operation import NetworkOperationStatus
from app.models.provisioning import ServiceOrder
from app.models.uisp_control import (
    UispConfigSnapshot,
    UispIntentStatus,
    UispIntentTargetType,
    UispSnapshotSource,
)
from app.services.uisp_control_plane import (
    UispIntentError,
    observe_intent,
    reconcile_inventory,
    request_apply,
    stage_from_service_order,
    stage_intent,
    update_intent_desired,
)


def _subscription(db_session, subscriber, catalog_offer):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _cpe(db_session, subscriber, subscription, *, uisp_id="uisp-radio-1"):
    device = CPEDevice(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        device_type=DeviceType.wireless_radio,
        uisp_device_id=uisp_id,
        mac_address="24:A4:3C:AA:BB:01",
    )
    db_session.add(device)
    db_session.flush()
    return device


def _observation(device_id="uisp-radio-1", *, ip="172.21.10.2/24"):
    return {
        "identification": {
            "id": device_id,
            "name": "CUST-RADIO-1",
            "model": "LBE-5AC-Gen2",
            "mac": "24:A4:3C:AA:BB:01",
            "firmwareVersion": "8.7.19",
        },
        "ipAddress": ip,
        "overview": {"status": "active"},
    }


def test_matching_observation_is_only_path_to_verified(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_config={"management_ip": "172.21.10.2"},
    )

    assert intent.status == UispIntentStatus.staged
    assert intent.verified_revision is None

    observe_intent(db_session, intent, _observation())

    assert intent.status == UispIntentStatus.verified
    assert intent.verified_revision == intent.desired_revision
    assert intent.last_verified_at is not None
    snapshots = (
        db_session.query(UispConfigSnapshot)
        .filter(UispConfigSnapshot.intent_id == intent.id)
        .order_by(UispConfigSnapshot.created_at)
        .all()
    )
    assert [snapshot.source for snapshot in snapshots] == [
        UispSnapshotSource.desired,
        UispSnapshotSource.observed,
    ]


def test_drift_is_reported_from_observed_inventory(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_config={"management_ip": "172.21.10.9"},
    )

    observe_intent(db_session, intent, _observation())

    assert intent.status == UispIntentStatus.drifted
    assert intent.drift["differences"]["management_ip"] == {
        "desired": "172.21.10.9",
        "observed": "172.21.10.2",
    }


def test_wifi_intent_is_manual_and_plaintext_secret_is_rejected(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)

    try:
        stage_intent(
            db_session,
            target_type=UispIntentTargetType.cpe,
            target_id=cpe.id,
            desired_config={"wifi": {"ssid": "Customer", "password": "unsafe"}},
        )
    except UispIntentError as exc:
        assert "password_ref" in str(exc)
    else:  # pragma: no cover - explicit security contract
        raise AssertionError("plaintext Wi-Fi secret was accepted")

    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_config={
            "wifi": {"ssid": "Customer", "password_ref": "bao://uisp/wifi/1"}
        },
    )
    observe_intent(db_session, intent, _observation())
    operation = request_apply(db_session, intent, initiated_by="test-admin")

    assert intent.status == UispIntentStatus.manual_required
    assert operation.status == NetworkOperationStatus.warning
    assert operation.output_payload["applied"] is False
    assert operation.error
    desired_snapshot = (
        db_session.query(UispConfigSnapshot)
        .filter(
            UispConfigSnapshot.intent_id == intent.id,
            UispConfigSnapshot.source == UispSnapshotSource.desired,
        )
        .order_by(UispConfigSnapshot.created_at.desc())
        .first()
    )
    assert desired_snapshot.config["wifi"]["password_ref"] == "[redacted]"


def test_inventory_reconcile_marks_missing_without_false_success(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_config={"management_ip": "172.21.10.2"},
    )

    result = reconcile_inventory(db_session, [])

    db_session.refresh(intent)
    assert result["missing"] == 1
    assert intent.status == UispIntentStatus.pending_observation
    assert intent.verified_revision is None


def test_inventory_reconcile_refreshes_post_adoption_uisp_binding(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription, uisp_id=None)
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_config={"management_ip": "172.21.10.2"},
    )
    assert intent.uisp_device_id is None

    cpe.uisp_device_id = "uisp-radio-1"
    db_session.flush()
    result = reconcile_inventory(db_session, [_observation()])

    db_session.refresh(intent)
    assert result["observed"] == 1
    assert intent.uisp_device_id == "uisp-radio-1"
    assert intent.status == UispIntentStatus.verified


def test_fixed_wireless_order_stages_exact_device_or_waits(
    db_session, subscriber, catalog_offer
):
    catalog_offer.access_type = AccessType.fixed_wireless
    subscription = _subscription(db_session, subscriber, catalog_offer)
    waiting_order = ServiceOrder(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        execution_context={"uisp_desired": {"name": "CUST-RADIO-1"}},
    )
    db_session.add(waiting_order)
    db_session.flush()

    assert stage_from_service_order(db_session, waiting_order) is None
    assert waiting_order.execution_context["uisp_control"]["status"] == (
        "awaiting_device_assignment"
    )

    cpe = _cpe(db_session, subscriber, subscription)
    intent = stage_from_service_order(db_session, waiting_order)

    assert intent is not None
    assert intent.target_id == cpe.id
    assert intent.subscription_id == subscription.id
    assert intent.service_order_id == waiting_order.id
    assert waiting_order.execution_context["uisp_control"]["intent_id"] == str(
        intent.id
    )


def test_admin_routes_expose_uisp_lifecycle():
    from app.web.admin.network_uisp_control import router

    paths = {route.path for route in router.routes}
    assert "/network/uisp-control" in paths
    assert "/network/uisp-control/{intent_id}" in paths
    assert "/network/uisp-control/{intent_id}/apply" in paths
    assert "/network/uisp-control/{intent_id}/desired" in paths


def test_operator_edit_stages_new_revision_without_verification(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_config={"management_ip": "172.21.10.2"},
    )
    observe_intent(db_session, intent, _observation())
    assert intent.status == UispIntentStatus.verified

    update_intent_desired(
        db_session,
        intent,
        firmware_version="8.7.20",
        wifi_ssid="Customer WiFi",
        remote_access_enabled=False,
    )

    assert intent.status == UispIntentStatus.staged
    assert intent.desired_revision == 2
    assert intent.verified_revision == 1
    assert intent.desired_config["firmware_version"] == "8.7.20"
