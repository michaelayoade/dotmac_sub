from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_translate_catalog_offer_prefers_radius_network_policy() -> None:
    from app.services.service_intent_adapter import translate_catalog_offer

    offer = SimpleNamespace(
        name="Fiber 100",
        code="FIBER100",
        service_type="internet",
        access_type="fiber",
        plan_category="internet",
        speed_download_mbps=100,
        speed_upload_mbps=50,
        guaranteed_speed_limit_at=80,
        priority="gold",
        burst_profile="burst-100",
        default_ont_profile_id=uuid4(),
    )
    profile_id = uuid4()
    radius_profile = SimpleNamespace(
        id=profile_id,
        name="GPON 200M",
        code="gpon-200",
        download_speed=200_000,
        upload_speed=100_000,
        vlan_id=203,
        inner_vlan_id=3003,
        ip_pool_name="pool-v4",
        ipv6_pool_name="pool-v6",
        mikrotik_rate_limit="200M/100M",
    )

    params = translate_catalog_offer(
        offer,
        radius_profile=radius_profile,
        provisioning_nas_device_id=uuid4(),
    )

    assert params.offer_code == "FIBER100"
    assert params.download_kbps == 200_000
    assert params.upload_kbps == 100_000
    assert params.qos_profile == "200M/100M"
    assert params.radius_profile_id == str(profile_id)
    assert params.s_vlan == 203
    assert params.c_vlan == 3003
    assert params.ip_pool_name == "pool-v4"
    assert params.default_ont_profile_id == str(offer.default_ont_profile_id)


def test_build_spec_from_subscription_returns_network_safe_payload() -> None:
    from app.services.service_intent_adapter import (
        build_network_payload_from_subscription,
        build_provisioning_spec_from_subscription,
    )

    subscriber_id = uuid4()
    subscription_id = uuid4()
    offer_id = uuid4()
    subscriber = SimpleNamespace(
        id=subscriber_id,
        account_number="CUST-1001",
        display_name="Ada Customer",
        email="ada@example.net",
        phone="+2348012345678",
    )
    offer = SimpleNamespace(
        id=offer_id,
        name="Fiber 50",
        code="FIBER50",
        service_type="internet",
        access_type="fiber",
        plan_category="internet",
        speed_download_mbps=50,
        speed_upload_mbps=20,
        guaranteed_speed_limit_at=None,
        priority=None,
        burst_profile=None,
        default_ont_profile_id=None,
    )
    subscription = SimpleNamespace(
        id=subscription_id,
        offer_id=offer_id,
        status="active",
        subscriber=subscriber,
        offer=offer,
        service_address=SimpleNamespace(
            id=uuid4(),
            street="1 GPON Way",
            city="Lagos",
            state="LA",
            postal_code="100001",
        ),
        service_address_id=None,
        provisioning_nas_device_id=None,
        radius_profile=None,
        radius_profile_id=None,
        login="CUST-1001@dotmac",
        ipv4_address="100.64.1.10",
        ipv6_address=None,
        mac_address="aa:bb:cc:dd:ee:ff",
        service_description="Primary internet",
    )

    spec = build_provisioning_spec_from_subscription(None, subscription)
    payload = build_network_payload_from_subscription(None, subscription)

    assert spec.subscription_id == str(subscription_id)
    assert spec.offer_id == str(offer_id)
    assert spec.subscriber.subscriber_id == str(subscriber_id)
    assert spec.subscriber.display_name == "Ada Customer"
    assert spec.network.download_kbps == 50_000
    assert spec.network.upload_kbps == 20_000
    assert spec.pppoe_login == "CUST-1001@dotmac"
    assert payload["subscriber"]["display_name"] == "Ada Customer"
    assert payload["network"]["offer_code"] == "FIBER50"
    assert "Subscriber(" not in repr(payload)


def test_build_olt_provisioning_spec_uses_scalar_intent_only() -> None:
    from app.services.service_intent_adapter import (
        ServiceIntentAdapter,
        SubscriberServiceRef,
        SubscriptionProvisioningSpec,
    )

    adapter = ServiceIntentAdapter()
    intent = SubscriptionProvisioningSpec(
        subscription_id="sub-1",
        offer_id="offer-1",
        status="active",
        subscriber=SubscriberServiceRef(
            subscriber_id="subscriber-1",
            display_name="Network Safe Name",
        ),
        network=adapter.translate_offer(
            SimpleNamespace(
                name="Fiber 20",
                code="FIBER20",
                service_type="internet",
                access_type="fiber",
                plan_category="internet",
                speed_download_mbps=20,
                speed_upload_mbps=10,
                guaranteed_speed_limit_at=None,
                priority=None,
                burst_profile=None,
                default_ont_profile_id=None,
            ),
            radius_profile=SimpleNamespace(
                id=uuid4(),
                name="VLAN 203",
                code="vlan203",
                download_speed=None,
                upload_speed=None,
                vlan_id=203,
                inner_vlan_id=None,
                ip_pool_name=None,
                ipv6_pool_name=None,
                mikrotik_rate_limit=None,
            ),
        ),
    )

    provisioning_spec = adapter.build_olt_provisioning_spec(intent, gem_index=2)

    assert len(provisioning_spec.wan_services) == 1
    wan_service = provisioning_spec.wan_services[0]
    assert wan_service.service_type == "internet"
    assert wan_service.vlan_id == 203
    assert wan_service.gem_index == 2
    assert wan_service.user_vlan == 203
