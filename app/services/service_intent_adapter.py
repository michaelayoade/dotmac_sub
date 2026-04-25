"""Translate catalog subscriptions into network provisioning intent.

This module is the boundary between commercial service definitions and network
operations. It accepts catalog/subscription-shaped objects and returns small
DTOs with only scalar network parameters, so OLT/ONT code does not need to know
about subscribers, offers, billing, or catalog models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.services.adapters import adapter_registry

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _enum_value(raw: object) -> str | None:
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    text = str(value or "").strip()
    return text or None


def _id(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _text(raw: object) -> str | None:
    text = str(raw or "").strip()
    return text or None


def _mbps_to_kbps(raw: object) -> int | None:
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value * 1000 if value > 0 else None


from app.services.network._util import first_present

def _first_present(*values: object) -> object | None:
    return first_present(*values, exclude_empty_list=True)


@dataclass(frozen=True)
class SubscriberServiceRef:
    """Subscriber identity reduced to fields safe for network operations."""

    subscriber_id: str | None = None
    account_number: str | None = None
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    service_address_id: str | None = None
    service_address_label: str | None = None


@dataclass(frozen=True)
class NetworkServiceParameters:
    """Network-facing parameters derived from a catalog offer and policy."""

    service_type: str | None = None
    access_type: str | None = None
    plan_category: str | None = None
    offer_code: str | None = None
    offer_name: str | None = None
    download_kbps: int | None = None
    upload_kbps: int | None = None
    guaranteed_speed_percent: int | None = None
    qos_profile: str | None = None
    burst_profile: str | None = None
    radius_profile_id: str | None = None
    radius_profile_code: str | None = None
    radius_profile_name: str | None = None
    s_vlan: int | None = None
    c_vlan: int | None = None
    ip_pool_name: str | None = None
    ipv6_pool_name: str | None = None
    provisioning_nas_device_id: str | None = None
    default_ont_profile_id: str | None = None

    @property
    def has_bandwidth_intent(self) -> bool:
        return self.download_kbps is not None or self.upload_kbps is not None

    @property
    def has_vlan_intent(self) -> bool:
        return self.s_vlan is not None or self.c_vlan is not None


@dataclass(frozen=True)
class SubscriptionProvisioningSpec:
    """Complete provisioning intent for one subscription."""

    subscription_id: str | None
    offer_id: str | None
    status: str | None
    subscriber: SubscriberServiceRef
    network: NetworkServiceParameters
    pppoe_login: str | None = None
    ipv4_address: str | None = None
    ipv6_address: str | None = None
    mac_address: str | None = None
    service_description: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_network_payload(self) -> dict[str, object]:
        """Return a primitive payload suitable for queues, tasks, and adapters."""
        return {
            "subscription_id": self.subscription_id,
            "offer_id": self.offer_id,
            "status": self.status,
            "subscriber": {
                "subscriber_id": self.subscriber.subscriber_id,
                "account_number": self.subscriber.account_number,
                "display_name": self.subscriber.display_name,
                "email": self.subscriber.email,
                "phone": self.subscriber.phone,
                "service_address_id": self.subscriber.service_address_id,
                "service_address_label": self.subscriber.service_address_label,
            },
            "network": {
                "service_type": self.network.service_type,
                "access_type": self.network.access_type,
                "plan_category": self.network.plan_category,
                "offer_code": self.network.offer_code,
                "offer_name": self.network.offer_name,
                "download_kbps": self.network.download_kbps,
                "upload_kbps": self.network.upload_kbps,
                "guaranteed_speed_percent": self.network.guaranteed_speed_percent,
                "qos_profile": self.network.qos_profile,
                "burst_profile": self.network.burst_profile,
                "radius_profile_id": self.network.radius_profile_id,
                "radius_profile_code": self.network.radius_profile_code,
                "radius_profile_name": self.network.radius_profile_name,
                "s_vlan": self.network.s_vlan,
                "c_vlan": self.network.c_vlan,
                "ip_pool_name": self.network.ip_pool_name,
                "ipv6_pool_name": self.network.ipv6_pool_name,
                "provisioning_nas_device_id": self.network.provisioning_nas_device_id,
                "default_ont_profile_id": self.network.default_ont_profile_id,
            },
            "pppoe_login": self.pppoe_login,
            "ipv4_address": self.ipv4_address,
            "ipv6_address": self.ipv6_address,
            "mac_address": self.mac_address,
            "service_description": self.service_description,
            "warnings": list(self.warnings),
        }


class ServiceIntentAdapter:
    """Build network service intent from catalog and subscription records."""

    name = "service_intent"

    def translate_offer(
        self,
        offer: object,
        *,
        radius_profile: object | None = None,
        provisioning_nas_device_id: object | None = None,
    ) -> NetworkServiceParameters:
        """Translate a catalog offer into network-facing parameters."""
        radius_download = getattr(radius_profile, "download_speed", None)
        radius_upload = getattr(radius_profile, "upload_speed", None)
        offer_download = getattr(offer, "speed_download_mbps", None)
        offer_upload = getattr(offer, "speed_upload_mbps", None)

        return NetworkServiceParameters(
            service_type=_enum_value(getattr(offer, "service_type", None)),
            access_type=_enum_value(getattr(offer, "access_type", None)),
            plan_category=_enum_value(getattr(offer, "plan_category", None)),
            offer_code=_text(getattr(offer, "code", None)),
            offer_name=_text(getattr(offer, "name", None)),
            download_kbps=int(radius_download)
            if radius_download not in (None, "")
            else _mbps_to_kbps(offer_download),
            upload_kbps=int(radius_upload)
            if radius_upload not in (None, "")
            else _mbps_to_kbps(offer_upload),
            guaranteed_speed_percent=getattr(offer, "guaranteed_speed_limit_at", None),
            qos_profile=_text(
                _first_present(
                    getattr(radius_profile, "mikrotik_rate_limit", None),
                    getattr(offer, "priority", None),
                )
            ),
            burst_profile=_text(getattr(offer, "burst_profile", None)),
            radius_profile_id=_id(getattr(radius_profile, "id", None)),
            radius_profile_code=_text(getattr(radius_profile, "code", None)),
            radius_profile_name=_text(getattr(radius_profile, "name", None)),
            s_vlan=getattr(radius_profile, "vlan_id", None),
            c_vlan=getattr(radius_profile, "inner_vlan_id", None),
            ip_pool_name=_text(getattr(radius_profile, "ip_pool_name", None)),
            ipv6_pool_name=_text(getattr(radius_profile, "ipv6_pool_name", None)),
            provisioning_nas_device_id=_id(provisioning_nas_device_id),
            default_ont_profile_id=_id(getattr(offer, "default_ont_profile_id", None)),
        )

    def build_from_subscription(
        self,
        db: Session | None,
        subscription: object,
        *,
        subscriber: object | None = None,
        offer: object | None = None,
        radius_profile: object | None = None,
    ) -> SubscriptionProvisioningSpec:
        """Build provisioning intent from a subscription-shaped object."""
        warnings: list[str] = []
        subscriber = subscriber or getattr(subscription, "subscriber", None)
        offer = offer or getattr(subscription, "offer", None)
        if offer is None and db is not None:
            offer = self._load_offer(db, getattr(subscription, "offer_id", None))
        if offer is None:
            warnings.append("Subscription has no catalog offer.")

        radius_profile = radius_profile or getattr(subscription, "radius_profile", None)
        if radius_profile is None and db is not None:
            radius_profile = self._load_radius_profile(
                db,
                subscription_radius_profile_id=getattr(
                    subscription, "radius_profile_id", None
                ),
                offer_id=getattr(subscription, "offer_id", None),
            )

        service_address = getattr(subscription, "service_address", None)
        subscriber_ref = self.build_subscriber_ref(
            subscriber,
            service_address=service_address,
            service_address_id=getattr(subscription, "service_address_id", None),
        )
        network = (
            self.translate_offer(
                offer,
                radius_profile=radius_profile,
                provisioning_nas_device_id=getattr(
                    subscription, "provisioning_nas_device_id", None
                ),
            )
            if offer is not None
            else NetworkServiceParameters(
                provisioning_nas_device_id=_id(
                    getattr(subscription, "provisioning_nas_device_id", None)
                )
            )
        )

        return SubscriptionProvisioningSpec(
            subscription_id=_id(getattr(subscription, "id", None)),
            offer_id=_id(getattr(subscription, "offer_id", None)),
            status=_enum_value(getattr(subscription, "status", None)),
            subscriber=subscriber_ref,
            network=network,
            pppoe_login=_text(getattr(subscription, "login", None)),
            ipv4_address=_text(getattr(subscription, "ipv4_address", None)),
            ipv6_address=_text(getattr(subscription, "ipv6_address", None)),
            mac_address=_text(getattr(subscription, "mac_address", None)),
            service_description=_text(getattr(subscription, "service_description", None)),
            warnings=tuple(warnings),
        )

    def build_from_subscription_id(
        self,
        db: Session,
        subscription_id: object,
    ) -> SubscriptionProvisioningSpec:
        """Load a subscription and build provisioning intent."""
        from app.models.catalog import Subscription

        subscription = db.get(Subscription, subscription_id)
        if subscription is None:
            raise ValueError(f"Subscription not found: {subscription_id}")
        return self.build_from_subscription(db, subscription)

    def build_olt_provisioning_spec(
        self,
        intent: SubscriptionProvisioningSpec,
        *,
        gem_index: int = 1,
        connection_type: str = "pppoe",
    ):
        """Build an OLT command generator spec from subscription intent.

        This is a narrow bridge for network writers that already consume
        ``ProvisioningSpec``. It uses only the scalar intent DTO; it does not
        pass catalog, subscriber, or subscription models into the network layer.
        """
        from app.services.network.olt_command_gen import (
            ProvisioningSpec,
            WanServiceSpec,
        )

        wan_services = []
        if intent.network.s_vlan:
            wan_services.append(
                WanServiceSpec(
                    service_type=intent.network.plan_category
                    or intent.network.service_type
                    or "internet",
                    vlan_id=int(intent.network.s_vlan),
                    gem_index=gem_index,
                    connection_type=connection_type,
                    c_vlan=intent.network.c_vlan,
                    user_vlan=intent.network.c_vlan or intent.network.s_vlan,
                    tag_transform="translate"
                    if intent.network.c_vlan
                    else "default",
                )
            )
        return ProvisioningSpec(wan_services=wan_services)

    def build_subscriber_ref(
        self,
        subscriber: object | None,
        *,
        service_address: object | None = None,
        service_address_id: object | None = None,
    ) -> SubscriberServiceRef:
        """Reduce a subscriber model to network-safe identity fields."""
        return SubscriberServiceRef(
            subscriber_id=_id(getattr(subscriber, "id", None)),
            account_number=_text(getattr(subscriber, "account_number", None)),
            display_name=_text(
                _first_present(
                    getattr(subscriber, "display_name", None),
                    " ".join(
                        part
                        for part in (
                            _text(getattr(subscriber, "first_name", None)),
                            _text(getattr(subscriber, "last_name", None)),
                        )
                        if part
                    ),
                )
            ),
            email=_text(getattr(subscriber, "email", None)),
            phone=_text(
                _first_present(
                    getattr(subscriber, "phone", None),
                    getattr(subscriber, "phone_number", None),
                )
            ),
            service_address_id=_id(
                _first_present(service_address_id, getattr(service_address, "id", None))
            ),
            service_address_label=self._format_address(service_address),
        )

    def _load_offer(self, db: Session, offer_id: object) -> object | None:
        if offer_id is None:
            return None
        from app.models.catalog import CatalogOffer

        return db.get(CatalogOffer, offer_id)

    def _load_radius_profile(
        self,
        db: Session,
        *,
        subscription_radius_profile_id: object | None,
        offer_id: object | None,
    ) -> object | None:
        if subscription_radius_profile_id:
            from app.models.catalog import RadiusProfile

            return db.get(RadiusProfile, subscription_radius_profile_id)
        if offer_id is None:
            return None
        from app.models.catalog import OfferRadiusProfile

        link = db.scalars(
            select(OfferRadiusProfile)
            .where(OfferRadiusProfile.offer_id == offer_id)
            .limit(1)
        ).first()
        return getattr(link, "profile", None) if link else None

    def _format_address(self, address: object | None) -> str | None:
        if address is None:
            return None
        direct = _text(getattr(address, "full_address", None))
        if direct:
            return direct
        parts = [
            _text(getattr(address, "street", None)),
            _text(getattr(address, "city", None)),
            _text(getattr(address, "state", None)),
            _text(getattr(address, "postal_code", None)),
        ]
        return ", ".join(part for part in parts if part) or None


service_intent_adapter = ServiceIntentAdapter()
adapter_registry.register(service_intent_adapter)


def translate_catalog_offer(
    offer: object,
    *,
    radius_profile: object | None = None,
    provisioning_nas_device_id: object | None = None,
) -> NetworkServiceParameters:
    return service_intent_adapter.translate_offer(
        offer,
        radius_profile=radius_profile,
        provisioning_nas_device_id=provisioning_nas_device_id,
    )


def build_provisioning_spec_from_subscription(
    db: Session | None,
    subscription: object,
    *,
    subscriber: object | None = None,
    offer: object | None = None,
    radius_profile: object | None = None,
) -> SubscriptionProvisioningSpec:
    return service_intent_adapter.build_from_subscription(
        db,
        subscription,
        subscriber=subscriber,
        offer=offer,
        radius_profile=radius_profile,
    )


def build_network_payload_from_subscription(
    db: Session | None,
    subscription: object,
    *,
    subscriber: object | None = None,
    offer: object | None = None,
    radius_profile: object | None = None,
) -> dict[str, object]:
    return build_provisioning_spec_from_subscription(
        db,
        subscription,
        subscriber=subscriber,
        offer=offer,
        radius_profile=radius_profile,
    ).as_network_payload()
