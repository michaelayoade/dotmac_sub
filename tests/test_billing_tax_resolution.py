"""One compatibility owner selects VAT until dotmac-tax cutover."""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import TaxApplication, TaxRate
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.services.billing_tax_resolution import (
    BillingTaxSource,
    resolve_subscription_tax,
    resolve_subscription_taxes,
)


def test_customer_exemption_is_the_highest_precedence_tax_fact(
    db_session, subscription, subscriber
):
    rate = TaxRate(
        name="Account VAT",
        code="ACCOUNT-VAT-RESOLUTION",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(rate)
    db_session.flush()
    subscriber.tax_rate_id = rate.id
    subscription.offer.with_vat = True
    db_session.add(
        CustomerTaxPolicy(
            account_id=subscriber.id,
            withholding_tax_enabled=False,
            vat_exempt=True,
            version=7,
            updated_by="pytest",
        )
    )
    db_session.commit()

    resolved = resolve_subscription_tax(db_session, subscription)

    assert resolved.tax_rate_id is None
    assert resolved.tax_application == TaxApplication.exempt
    assert resolved.source == BillingTaxSource.customer_vat_exemption
    assert resolved.customer_tax_policy_version == 7


def test_batch_resolution_returns_one_typed_result_per_subscription(
    db_session, subscription, subscriber
):
    rate = TaxRate(
        name="Account VAT",
        code="ACCOUNT-VAT-BATCH",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(rate)
    db_session.flush()
    subscriber.tax_rate_id = rate.id
    db_session.commit()

    resolved = resolve_subscription_taxes(db_session, [subscription])

    assert tuple(resolved) == (subscription.id,)
    assert resolved[subscription.id].tax_rate_id == rate.id
    assert resolved[subscription.id].source == BillingTaxSource.account_tax_rate
