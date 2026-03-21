from decimal import Decimal

from app.models.billing import TaxRate
from app.models.subscriber import Organization, Subscriber
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


def test_update_organization_customer_applies_billing_overrides_to_linked_subscribers(db_session):
    tax_rate = TaxRate(name="Reduced VAT", rate=Decimal("0.050"))
    organization = Organization(name="Acme Corp")
    db_session.add_all([tax_rate, organization])
    db_session.flush()
    first = Subscriber(first_name="A", last_name="One", email="a.one@example.com", organization_id=organization.id)
    second = Subscriber(first_name="B", last_name="Two", email="b.two@example.com", organization_id=organization.id)
    db_session.add_all([first, second])
    db_session.commit()

    actions.update_organization_customer(
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
        tax_rate_id=str(tax_rate.id),
        payment_method="cash",
    )

    refreshed = (
        db_session.query(Subscriber)
        .filter(Subscriber.organization_id == organization.id)
        .order_by(Subscriber.email.asc())
        .all()
    )
    assert len(refreshed) == 2
    for subscriber in refreshed:
        assert subscriber.billing_enabled is True
        assert subscriber.billing_day == 12
        assert subscriber.payment_due_days == 9
        assert subscriber.grace_period_days == 4
        assert subscriber.min_balance == Decimal("75.00")
        assert subscriber.tax_rate_id == tax_rate.id
        assert subscriber.payment_method == "cash"
