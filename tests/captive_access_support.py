"""Shared builders for captive access policy tests (no test functions here).

Everything is created through the real models; router readiness comes from a
real ``router_config_snapshots`` export rendered from the current settings, so
the gate under test is the production readiness path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.captive_access_policy import (
    CaptiveAccessRule,
    CaptiveCustomerSet,
    CaptiveCustomerSetMember,
)
from app.models.catalog import NasDevice, Subscription
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius_active_session import RadiusActiveSession
from app.models.router_management import (
    Router,
    RouterConfigSnapshot,
    RouterSnapshotSource,
)
from app.models.subscriber import (
    Reseller,
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.models.subscription_engine import SettingValueType
from app.services.walled_garden_router_module import (
    WalledGardenElement,
    render_walled_garden_module_from_settings,
)

PORTAL_URL = "https://portal.example.test/pay"
PORTAL_IP = "203.0.113.10"

_SECTIONS = {
    "address_list": "/ip firewall address-list",
    "filter": "/ip firewall filter",
    "nat": "/ip firewall nat",
}


def set_radius_setting(
    db: Session, key: str, value: str, value_type: SettingValueType
) -> None:
    db.query(DomainSetting).filter(
        DomainSetting.domain == SettingDomain.radius,
        DomainSetting.key == key,
    ).delete(synchronize_session=False)
    db.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key=key,
            value_type=value_type,
            value_text=value,
            value_json=value.lower() == "true"
            if value_type is SettingValueType.boolean
            else None,
            is_active=True,
        )
    )
    db.flush()


def ready_network(db: Session) -> None:
    set_radius_setting(db, "captive_redirect_enabled", "true", SettingValueType.boolean)
    set_radius_setting(db, "captive_portal_ip", PORTAL_IP, SettingValueType.string)
    set_radius_setting(db, "captive_portal_url", PORTAL_URL, SettingValueType.string)


def house_reseller(db: Session, *, code: str | None = None) -> Reseller:
    existing = db.query(Reseller).filter(Reseller.is_house.is_(True)).first()
    if existing is not None:
        return existing
    reseller = Reseller(
        name="House",
        code=code or f"HOUSE-{uuid.uuid4().hex[:6]}",
        is_house=True,
        is_active=True,
    )
    db.add(reseller)
    db.flush()
    return reseller


def residential_house_account(db: Session, account: Subscriber) -> Subscriber:
    account.reseller_id = house_reseller(db).id
    account.user_type = UserType.customer
    account.category = SubscriberCategory.residential
    account.is_active = True
    account.status = SubscriberStatus.active
    db.flush()
    return account


def _export_value(value: str) -> str:
    if any(ch in value for ch in ' ()"') or ":" in value or "," in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _line(element: WalledGardenElement) -> str:
    props = {**dict(element.fields), "comment": element.tag}
    return "add " + " ".join(
        f"{key}={_export_value(value)}" for key, value in sorted(props.items())
    )


def module_export(db: Session) -> str:
    """A RouterOS export carrying exactly the module rendered from settings."""

    module = render_walled_garden_module_from_settings(db)
    lines: list[str] = []
    for resource, section in _SECTIONS.items():
        lines.append(section)
        lines.extend(
            _line(element)
            for element in module.elements
            if element.resource.value == resource
        )
    return "\n".join(lines)


def nas_with_router(
    db: Session,
    *,
    ready: bool = True,
    name: str | None = None,
    captured_at: datetime | None = None,
    nas_ip: str | None = None,
) -> tuple[NasDevice, Router]:
    label = name or f"BNG-{uuid.uuid4().hex[:6]}"
    nas = NasDevice(name=label, nas_ip=nas_ip)
    db.add(nas)
    db.flush()
    router = Router(
        name=label,
        hostname=label.lower(),
        management_ip="192.0.2.1",
        rest_api_username="readonly",
        rest_api_password="not-used-in-tests",  # noqa: S106 - fixture only
        is_active=True,
        nas_device_id=nas.id,
    )
    db.add(router)
    db.flush()
    db.add(
        RouterConfigSnapshot(
            router_id=router.id,
            config_export=module_export(db) if ready else "/ip firewall filter\n",
            config_hash="0" * 64,
            source=RouterSnapshotSource.scheduled,
            created_at=captured_at or datetime.now(UTC),
        )
    )
    db.flush()
    return nas, router


def serve_from(db: Session, subscription: Subscription, nas: NasDevice) -> None:
    subscription.provisioning_nas_device_id = nas.id
    db.flush()


def open_session(
    db: Session,
    subscription: Subscription,
    *,
    nas: NasDevice | None = None,
    nas_ip: str | None = None,
) -> RadiusActiveSession:
    row = RadiusActiveSession(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        nas_device_id=nas.id if nas is not None else None,
        nas_ip_address=nas_ip,
        username=f"user-{uuid.uuid4().hex[:6]}",
        acct_session_id=uuid.uuid4().hex,
        session_start=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def add_rule(
    db: Session,
    *,
    scope: str,
    effect: str = "allow",
    subscriber_id: uuid.UUID | None = None,
    customer_set_id: uuid.UUID | None = None,
    plan_family: str | None = None,
    offer_ids: list[str] | None = None,
    categories: list[str] | None = None,
    reseller_condition: str = "any",
    reseller_ids: list[str] | None = None,
    created_at: datetime | None = None,
) -> CaptiveAccessRule:
    now = created_at or datetime.now(UTC)
    rule = CaptiveAccessRule(
        scope=scope,
        effect=effect,
        subscriber_id=subscriber_id,
        customer_set_id=customer_set_id,
        plan_family=plan_family,
        offer_ids=offer_ids,
        subscriber_categories=categories,
        reseller_condition=reseller_condition,
        reseller_ids=reseller_ids,
        enabled=True,
        created_by="test",
        reason="test rule for captive policy",
        created_at=now,
        updated_at=now,
    )
    db.add(rule)
    db.flush()
    return rule


def customer_set(
    db: Session, *, name: str, members: tuple[uuid.UUID, ...] = ()
) -> CaptiveCustomerSet:
    cohort = CaptiveCustomerSet(
        name=name,
        is_active=True,
        created_by="test",
        reason="test cohort for captive policy",
    )
    db.add(cohort)
    db.flush()
    for subscriber_id in members:
        db.add(
            CaptiveCustomerSetMember(
                customer_set_id=cohort.id,
                subscriber_id=subscriber_id,
                added_by="test",
                added_reason="test membership",
            )
        )
    db.flush()
    return cohort
