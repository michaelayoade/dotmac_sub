from decimal import Decimal

from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from scripts.migration import phase1_customers_services as phase1
from scripts.migration import phase3_operational_data as phase3


def test_map_customer_status_matches_splynx_1to1():
    assert phase1._map_customer_status("new", is_deleted=False) == SubscriberStatus.new
    assert (
        phase1._map_customer_status("active", is_deleted=False)
        == SubscriberStatus.active
    )
    assert (
        phase1._map_customer_status("blocked", is_deleted=False)
        == SubscriberStatus.blocked
    )
    assert (
        phase1._map_customer_status("disabled", is_deleted=False)
        == SubscriberStatus.disabled
    )
    assert (
        phase1._map_customer_status("active", is_deleted=True)
        == SubscriberStatus.canceled
    )


def test_map_service_status_matches_splynx_1to1():
    assert (
        phase1._map_service_status("active", is_deleted=False)
        == SubscriptionStatus.active
    )
    assert (
        phase1._map_service_status("blocked", is_deleted=False)
        == SubscriptionStatus.blocked
    )
    assert (
        phase1._map_service_status("disabled", is_deleted=False)
        == SubscriptionStatus.disabled
    )
    assert (
        phase1._map_service_status("hidden", is_deleted=False)
        == SubscriptionStatus.hidden
    )
    assert (
        phase1._map_service_status("stopped", is_deleted=False)
        == SubscriptionStatus.stopped
    )
    assert (
        phase1._map_service_status("active", is_deleted=True)
        == SubscriptionStatus.canceled
    )


def test_map_billing_mode_uses_splynx_billing_type():
    assert phase1._map_billing_mode("recurring") == BillingMode.postpaid
    assert phase1._map_billing_mode("prepaid") == BillingMode.prepaid
    assert phase1._map_billing_mode(None) == BillingMode.prepaid


def test_credit_note_application_amount_prefers_explicit_amount():
    amount = phase3._resolve_credit_note_application_amount(
        {"amount": "12.50"},
        credit_note_total=Decimal("100.00"),
        application_count=3,
    )

    assert amount == Decimal("12.50")


def test_credit_note_application_amount_only_falls_back_for_single_application():
    amount = phase3._resolve_credit_note_application_amount(
        {},
        credit_note_total=Decimal("100.00"),
        application_count=1,
    )
    ambiguous = phase3._resolve_credit_note_application_amount(
        {},
        credit_note_total=Decimal("100.00"),
        application_count=2,
    )

    assert amount == Decimal("100.00")
    assert ambiguous is None
