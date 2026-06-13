"""Splynx billing_type -> DotMac BillingMode mapping."""

from app.models.catalog import BillingMode
from app.services.migrations.billing_modes import map_billing_mode


def test_recurring_is_postpaid():
    assert map_billing_mode("recurring") == BillingMode.postpaid
    assert map_billing_mode("RECURRING") == BillingMode.postpaid
    assert map_billing_mode(" Recurring ") == BillingMode.postpaid


def test_prepaid_variants_are_prepaid():
    assert map_billing_mode("prepaid") == BillingMode.prepaid
    assert map_billing_mode("prepaid_monthly") == BillingMode.prepaid


def test_unknown_and_empty_default_to_prepaid():
    # The vast majority of the base is prepaid; an unrecognised or missing
    # type must never silently become postpaid (which would dun a prepaid
    # customer), so prepaid is the safe default.
    assert map_billing_mode(None) == BillingMode.prepaid
    assert map_billing_mode("") == BillingMode.prepaid
    assert map_billing_mode("something_new") == BillingMode.prepaid
