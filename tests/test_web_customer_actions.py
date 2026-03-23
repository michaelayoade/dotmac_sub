from decimal import Decimal

from app.models.billing import TaxRate
from app.models.auth import UserCredential
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Subscriber, SubscriberCategory, SubscriberStatus
from app.services.account_lifecycle import has_active_lock
from app.services import web_customer_actions as actions


def test_update_person_customer_persists_billing_overrides(db_session, subscriber):
    tax_rate = TaxRate(name="VAT", rate=Decimal("0.075"))
    db_session.add(tax_rate)
    db_session.commit()

    actions.update_person_customer(
        db=db_session,
        customer_id=str(subscriber.id),
        first_name="Test",
        last_name="User",
        display_name=None,
        avatar_url=None,
        email=subscriber.email,
        email_verified="false",
        phone=None,
        date_of_birth=None,
        gender="unknown",
        preferred_contact_method=None,
        locale=None,
        timezone_value=None,
        address_line1=None,
        address_line2=None,
        city=None,
        region=None,
        postal_code=None,
        country_code=None,
        status="active",
        is_active="true",
        marketing_opt_in="false",
        notes=None,
        account_start_date=None,
        billing_enabled_override="false",
        billing_day="8",
        payment_due_days="5",
        grace_period_days="2",
        min_balance="125.50",
        captive_redirect_enabled="false",
        tax_rate_id=str(tax_rate.id),
        payment_method="transfer",
        metadata_json=None,
    )

    updated = db_session.get(Subscriber, subscriber.id)
    assert updated is not None
    assert updated.billing_enabled is False
    assert updated.billing_day == 8
    assert updated.payment_due_days == 5
    assert updated.grace_period_days == 2
    assert updated.min_balance == Decimal("125.50")
    assert updated.tax_rate_id == tax_rate.id
    assert updated.payment_method == "transfer"


def test_update_business_customer_applies_billing_overrides_to_linked_subscribers(db_session):
    tax_rate = TaxRate(name="Reduced VAT", rate=Decimal("0.050"))
    organization = Subscriber(
        first_name="Acme",
        last_name="Business",
        email="billing@acme.example.com",
        company_name="Acme Corp",
    )
    organization.category = SubscriberCategory.business
    db_session.add_all([tax_rate, organization])
    db_session.flush()
    db_session.commit()

    actions.update_business_customer(
        db=db_session,
        customer_id=str(organization.id),
        name="Acme Corp",
        legal_name=None,
        tax_id=None,
        domain=None,
        website=None,
        org_notes=None,
        org_account_start_date=None,
        billing_enabled_override="true",
        billing_day="12",
        payment_due_days="9",
        grace_period_days="4",
        min_balance="75.00",
        captive_redirect_enabled="false",
        tax_rate_id=str(tax_rate.id),
        payment_method="cash",
    )

    refreshed = db_session.get(Subscriber, organization.id)
    assert refreshed is not None
    assert refreshed.company_name == "Acme Corp"
    assert refreshed.billing_enabled is True
    assert refreshed.billing_day == 12
    assert refreshed.payment_due_days == 9
    assert refreshed.grace_period_days == 4
    assert refreshed.min_balance == Decimal("75.00")
    assert refreshed.tax_rate_id == tax_rate.id
    assert refreshed.payment_method == "cash"


def test_deactivate_business_customer_suspends_member_subscriptions(db_session):
    offer = CatalogOffer(
        name="Business Fiber",
        service_type=ServiceType.business,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()

    subscriber = Subscriber(
        first_name="Acme",
        last_name="Business",
        email="org.member@example.com",
        company_name="Acme Corp",
        status=SubscriberStatus.active,
    )
    subscriber.category = SubscriberCategory.business
    db_session.add(subscriber)
    db_session.flush()

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.add(
        UserCredential(
            subscriber_id=subscriber.id,
            username="org.member@example.com",
            password_hash="hash",
        )
    )
    db_session.commit()

    actions.deactivate_business_customer(db_session, str(subscriber.id))
    db_session.refresh(subscriber)
    db_session.refresh(subscription)

    assert subscriber.is_active is False
    assert subscriber.status == SubscriberStatus.suspended
    assert subscription.status == SubscriptionStatus.suspended
    assert has_active_lock(
        db_session, str(subscription.id), EnforcementReason.admin
    )


def test_bulk_activate_business_restores_admin_locked_member(db_session):
    offer = CatalogOffer(
        name="Business Fiber",
        service_type=ServiceType.business,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()

    subscriber = Subscriber(
        first_name="Beta",
        last_name="Business",
        email="beta.member@example.com",
        company_name="Beta Corp",
        status=SubscriberStatus.active,
        is_active=True,
    )
    subscriber.category = SubscriberCategory.business
    db_session.add(subscriber)
    db_session.flush()

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.commit()

    actions.deactivate_business_customer(db_session, str(subscriber.id))
    result = actions.bulk_update_customer_status(
        db_session, [{"id": str(subscriber.id), "type": "business"}], True
    )

    db_session.refresh(subscriber)
    db_session.refresh(subscription)

    assert result["errors"] == []
    assert subscriber.is_active is True
    assert subscriber.status == SubscriberStatus.active
    assert subscription.status == SubscriptionStatus.active
    assert not has_active_lock(
        db_session, str(subscription.id), EnforcementReason.admin
    )
