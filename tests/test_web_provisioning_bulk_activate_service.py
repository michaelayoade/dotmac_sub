from __future__ import annotations

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessType,
    BillingCycle,
    CatalogOffer,
    OfferStatus,
    PlanCategory,
    PriceBasis,
    ServiceType,
    Subscription,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services import web_provisioning_bulk_activate as bulk_service


def _create_offer(db_session, *, name: str, category: PlanCategory) -> CatalogOffer:
    offer = CatalogOffer(
        name=name,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        plan_category=category,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


def test_bulk_activation_preview_counts(db_session):
    reseller = Reseller(name="Partner A", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)

    s1 = Subscriber(
        first_name="Alice",
        last_name="Preview",
        email="alice-preview@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    s2 = Subscriber(
        first_name="Bob",
        last_name="Preview",
        email="bob-preview@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    db_session.add(s1)
    db_session.add(s2)
    db_session.commit()

    offer = _create_offer(db_session, name="Internet Plan", category=PlanCategory.internet)
    filters = bulk_service.BulkFilters(
        tab="internet",
        reseller_id=str(reseller.id),
        subscriber_status="suspended",
        pop_site_id=None,
        date_from=None,
        date_to=None,
        custom_attr_key=None,
        custom_attr_value=None,
    )
    mapping = bulk_service.BulkMapping(
        offer_id=str(offer.id),
        activation_date=None,
        nas_device_id=None,
        ipv4_assignment="dynamic",
        static_ipv4=None,
        mac_address=None,
        login_prefix="sub-",
        login_suffix=None,
        service_password_mode="auto",
        service_password_manual=None,
        skip_active_service_check=False,
        set_subscribers_active=True,
    )

    preview = bulk_service.build_preview(db_session, filters=filters, mapping=mapping)
    assert preview["total_matches"] == 2
    assert preview["counts"]["create"] == 2


def test_bulk_activation_execute_creates_subscriptions_and_audit(db_session):
    reseller = Reseller(name="Partner B", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)

    subscriber = Subscriber(
        first_name="Charlie",
        last_name="Execute",
        email="charlie-execute@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)

    offer = _create_offer(db_session, name="Recurring Plan", category=PlanCategory.recurring)
    filters = bulk_service.BulkFilters(
        tab="recurring",
        reseller_id=str(reseller.id),
        subscriber_status="suspended",
        pop_site_id=None,
        date_from=None,
        date_to=None,
        custom_attr_key=None,
        custom_attr_value=None,
    )
    mapping = bulk_service.BulkMapping(
        offer_id=str(offer.id),
        activation_date=None,
        nas_device_id=None,
        ipv4_assignment="dynamic",
        static_ipv4=None,
        mac_address="AA:BB:CC:DD:EE:FF",
        login_prefix="isp-",
        login_suffix="-x",
        service_password_mode="manual",
        service_password_manual="SecretPass123",
        skip_active_service_check=False,
        set_subscribers_active=True,
    )

    job = bulk_service.create_job(
        db_session,
        filters=filters,
        mapping=mapping,
        actor_id=str(subscriber.id),
    )
    result = bulk_service.execute_job(db_session, job_id=str(job["job_id"]))
    assert result["status"] in {"completed", "partial"}

    created = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .all()
    )
    assert created
    assert created[0].offer_id == offer.id
    assert created[0].mac_address == "AA:BB:CC:DD:EE:FF"

    db_session.refresh(subscriber)
    assert subscriber.status == SubscriberStatus.active

    audit_rows = db_session.query(AuditEvent).filter(AuditEvent.action == "bulk_activate").all()
    assert audit_rows
