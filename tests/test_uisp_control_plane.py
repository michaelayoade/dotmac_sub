"""UISP desired/observed control-plane contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.catalog import AccessType, Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceType, VendorModelCapability
from app.models.network_operation import NetworkOperation, NetworkOperationStatus
from app.models.provisioning import ServiceOrder
from app.models.uisp_control import (
    UispConfigSnapshot,
    UispIntentStatus,
    UispIntentTargetType,
    UispSnapshotSource,
)
from app.services.control_plane_identity_view import uisp_identity_view
from app.services.uisp_control_plane import (
    UispIntentError,
    list_intents,
    observe_intent,
    prune_unsupported_desired,
    reconcile_inventory,
    request_apply,
    stage_from_service_order,
    stage_intent,
    update_intent_desired,
)
from app.services.uisp_write_adapter import UispCapabilityProfile, capability_profile
from app.web.templates import templates


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


def _capability(db_session, cpe, fields):
    cpe.vendor = "Ubiquiti"
    cpe.model = "LBE-5AC-Gen2"
    capability = VendorModelCapability(
        vendor="Ubiquiti",
        model="LBE-5AC-Gen2",
        supported_features={
            "uisp": {
                "configuration_write": True,
                "transport": "airos",
                "fields": fields,
            }
        },
    )
    db_session.add(capability)
    db_session.flush()
    return capability


def test_matching_observation_is_only_path_to_verified(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={"management_ip": "172.21.10.2"},
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
        desired_state={"management_ip": "172.21.10.9"},
    )

    observe_intent(db_session, intent, _observation())

    assert intent.status == UispIntentStatus.drifted
    assert intent.drift["differences"]["management_ip"] == {
        "desired": "172.21.10.9",
        "observed": "172.21.10.2",
    }


def test_wifi_intent_queues_without_false_success_and_rejects_plaintext_secret(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    _capability(
        db_session,
        cpe,
        {
            "wifi.ssid": "/wireless/ssid",
            "wifi.password_ref": "/wireless/key",
        },
    )

    try:
        stage_intent(
            db_session,
            target_type=UispIntentTargetType.cpe,
            target_id=cpe.id,
            desired_state={"wifi": {"ssid": "Customer", "password": "unsafe"}},
        )
    except UispIntentError as exc:
        assert "password_ref" in str(exc)
    else:  # pragma: no cover - explicit security contract
        raise AssertionError("plaintext Wi-Fi secret was accepted")

    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={
            "wifi": {"ssid": "Customer", "password_ref": "bao://uisp/wifi/1"}
        },
    )
    observe_intent(db_session, intent, _observation())
    operation = request_apply(
        db_session, intent, initiated_by="test-admin", enqueue=False
    )

    assert intent.status == UispIntentStatus.applying
    assert operation.status == NetworkOperationStatus.pending
    assert operation.output_payload is None
    assert operation.error is None
    binding = operation.input_payload["_adapter_binding"]
    assert binding["adapter_name"] == "uisp-airos"
    assert binding["capability_id"]
    assert binding["capability_revision"]
    assert binding["identity"]["model"] == "LBE-5AC-Gen2"
    assert binding["binding_fingerprint"]
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
        desired_state={"management_ip": "172.21.10.2"},
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
        desired_state={"management_ip": "172.21.10.2"},
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
    assert "/network/uisp-control/{intent_id}/desired/prune" in paths


def test_operator_edit_stages_new_revision_without_verification(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={"management_ip": "172.21.10.2"},
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
    assert intent.desired_state["firmware_version"] == "8.7.20"


def test_apply_rejects_stale_operator_revision(db_session, subscriber, catalog_offer):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    _capability(db_session, cpe, {"management_ip": "/interface/mgmt-ip"})
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={"management_ip": "172.21.10.2"},
    )
    update_intent_desired(db_session, intent, management_ip="172.21.10.3")

    try:
        request_apply(
            db_session,
            intent,
            expected_revision=1,
            enqueue=False,
        )
    except UispIntentError as exc:
        assert "current revision is 2" in str(exc)
    else:  # pragma: no cover - explicit stale-write safety contract
        raise AssertionError("stale UISP revision was queued")


def test_apply_preflight_blocks_unmapped_fields_before_operation_creation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    _capability(db_session, cpe, {"name": "/system/hostname"})
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={"name": "customer-radio", "remote_access": {"enabled": True}},
    )

    try:
        request_apply(db_session, intent, enqueue=False)
    except UispIntentError as exc:
        assert "remote_access.enabled" in str(exc)
    else:  # pragma: no cover - explicit no-write contract
        raise AssertionError("unsupported desired state was queued")

    assert intent.status == UispIntentStatus.manual_required
    assert db_session.query(NetworkOperation).count() == 0


def test_stage_rejects_malformed_nested_control_fields(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)

    for desired in (
        {"remote_access": {"enabled": "yes"}},
        {"remote_access": {"port": 22}},
        {"lifecycle": {"state": "unknown"}},
        {"wifi": {"ssid": ""}},
    ):
        try:
            stage_intent(
                db_session,
                target_type=UispIntentTargetType.cpe,
                target_id=cpe.id,
                desired_state=desired,
            )
        except UispIntentError:
            continue
        raise AssertionError(f"malformed desired state was accepted: {desired}")


def test_prune_unsupported_fields_stages_explicit_mapped_revision(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    _capability(db_session, cpe, {"name": "/system/hostname"})
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={
            "name": "customer-radio",
            "remote_access": {"enabled": True},
            "lifecycle": {"state": "active"},
        },
    )

    pruned = prune_unsupported_desired(db_session, intent)
    profile = capability_profile(db_session, pruned)

    assert pruned.desired_revision == 2
    assert pruned.desired_state == {"name": "customer-radio"}
    assert pruned.status == UispIntentStatus.staged
    assert profile.apply_ready is True


def test_model_mapped_form_does_not_inject_unsupported_defaults(
    db_session, subscriber, catalog_offer
):
    from app.web.admin.network_uisp_control import uisp_control_update_desired

    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription)
    _capability(db_session, cpe, {"name": "/system/hostname"})
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={"name": "old-name"},
    )

    response = uisp_control_update_desired(
        intent.id,
        name="new-name",
        management_ip=None,
        firmware_version=None,
        wifi_ssid=None,
        wifi_password=None,
        remote_access_enabled=None,
        lifecycle_state=None,
        db=db_session,
    )

    db_session.refresh(intent)
    assert response.status_code == 303
    assert intent.desired_state == {"name": "new-name"}


def test_hostname_only_model_renders_only_verified_control() -> None:
    profile = UispCapabilityProfile(
        vendor="Ubiquiti",
        model="LBE-5AC-Gen2",
        transport="airos",
        writable_fields=("name",),
        requested_fields=("name",),
        unsupported_fields=(),
    )
    intent = SimpleNamespace(
        id="intent-1",
        target_type=UispIntentTargetType.cpe,
        target_id="target-1",
        uisp_device_id="uisp-1",
        desired_revision=1,
        status=UispIntentStatus.staged,
        desired_state={"name": "radio-1"},
        observed_config={},
        drift={},
        snapshots=[],
    )

    html = templates.env.get_template("admin/network/uisp-control/detail.html").render(
        request=SimpleNamespace(
            state=SimpleNamespace(csrf_token="csrf"), query_params={}
        ),
        current_user={"name": "Admin", "email": "admin@example.test"},
        sidebar_stats={},
        active_page="uisp-control",
        active_menu="network",
        intent=intent,
        desired_redacted=intent.desired_state,
        capability_profile=profile,
        capability_error=None,
    )

    assert 'name="name"' in html
    assert 'name="wifi_ssid"' not in html
    assert 'name="wifi_password"' not in html
    assert 'name="remote_access_enabled"' not in html
    assert 'name="lifecycle_state"' not in html
    assert 'name="firmware_version"' not in html


def test_attention_filters_separate_drift_stale_and_write_blocked(
    db_session, subscriber, catalog_offer
) -> None:
    subscription = _subscription(db_session, subscriber, catalog_offer)
    rows = []
    for index in range(3):
        cpe = _cpe(
            db_session,
            subscriber,
            subscription,
            uisp_id=f"uisp-filter-{index}",
        )
        rows.append(
            stage_intent(
                db_session,
                target_type=UispIntentTargetType.cpe,
                target_id=cpe.id,
                desired_state={"name": f"radio-{index}"},
            )
        )

    rows[0].status = UispIntentStatus.drifted
    rows[0].last_observed_at = datetime.now(UTC)
    rows[1].last_observed_at = datetime.now(UTC) - timedelta(days=2)
    rows[2].status = UispIntentStatus.manual_required
    rows[2].last_observed_at = datetime.now(UTC)
    db_session.flush()

    assert [row.id for row in list_intents(db_session, health="drift")] == [rows[0].id]
    assert [row.id for row in list_intents(db_session, health="stale")] == [rows[1].id]
    assert [row.id for row in list_intents(db_session, health="unmapped")] == [
        rows[2].id
    ]


def test_identity_change_exposes_confirmed_replan_action(
    db_session, subscriber, catalog_offer
) -> None:
    subscription = _subscription(db_session, subscriber, catalog_offer)
    cpe = _cpe(db_session, subscriber, subscription, uisp_id="uisp-replan")
    _capability(db_session, cpe, {"name": "/system/hostname"})
    cpe.firmware_version = "8.7.19"
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={"name": "radio-replan"},
    )
    operation = request_apply(db_session, intent, enqueue=False)
    operation.status = NetworkOperationStatus.failed
    intent.status = UispIntentStatus.drifted
    cpe.firmware_version = "8.7.20"
    db_session.flush()
    profile = capability_profile(db_session, intent)
    identity = uisp_identity_view(
        db_session,
        intent,
        profile=profile,
        capability_error=None,
    )

    html = templates.env.get_template("admin/network/uisp-control/detail.html").render(
        request=SimpleNamespace(
            state=SimpleNamespace(csrf_token="csrf"), query_params={}
        ),
        current_user={"name": "Admin", "email": "admin@example.test"},
        sidebar_stats={},
        active_page="uisp-control",
        active_menu="network",
        intent=intent,
        desired_redacted=intent.desired_state,
        capability_profile=profile,
        capability_error=None,
        identity_view=identity,
    )

    assert identity.binding_changed is True
    assert identity.write_allowed is True
    assert f"/admin/network/uisp-control/{intent.id}/replan" in html
    assert 'data-confirm-label="Re-plan and apply"' in html
