import json
from decimal import Decimal

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.models.subscriber import Address, Subscriber, SubscriberCategory
from app.services.web_customer_details import build_business_detail_snapshot, build_person_detail_snapshot


def _billing_setting(key: str, value: str) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.billing,
        key=key,
        value_type=SettingValueType.string,
        value_text=value,
        is_active=True,
    )


def test_person_detail_includes_billing_policy_override_snapshot(db_session, subscriber):
    db_session.add_all(
        [
            _billing_setting("billing_enabled", "true"),
            _billing_setting("billing_day", "1"),
            _billing_setting("payment_due_days", "14"),
            _billing_setting("minimum_balance", "0"),
        ]
    )
    subscriber.billing_day = 5
    subscriber.payment_due_days = 7
    subscriber.grace_period_days = 3
    subscriber.min_balance = Decimal("150.00")
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))
    rows = {row["key"]: row for row in context["billing_policy"]["rows"]}

    assert context["billing_policy"]["has_overrides"] is True
    assert rows["billing_day"]["effective"] == "Day 5"
    assert rows["billing_day"]["source"] == "Customer override"
    assert rows["payment_due_days"]["effective"] == "7 day(s)"
    assert rows["payment_due_days"]["global"] == "14 day(s)"
    assert rows["grace_period_days"]["effective"] == "3 day(s)"


def test_business_detail_marks_mixed_billing_policy(db_session, subscriber):
    db_session.add_all(
        [
            _billing_setting("billing_enabled", "true"),
            _billing_setting("payment_due_days", "14"),
        ]
    )
    subscriber.company_name = "Test Org"
    subscriber.legal_name = "Test Org Ltd"
    subscriber.tax_id = "RC-123"
    subscriber.domain = "test.example.com"
    subscriber.website = "https://test.example.com"
    subscriber.category = SubscriberCategory.business
    subscriber.payment_due_days = 21
    db_session.commit()

    context = build_business_detail_snapshot(db_session, str(subscriber.id))
    rows = {row["key"]: row for row in context["billing_policy"]["rows"]}

    assert context["organization"] is not None
    assert context["organization"].name == "Test Org"
    assert rows["payment_due_days"]["effective"] == "21 day(s)"
    assert rows["payment_due_days"]["source"] == "Customer override"


def test_person_detail_includes_json_safe_geocode_payload(db_session, subscriber):
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="123 Sample Street",
        address_line2="Suite 5",
        city="Lagos",
        region="LA",
        postal_code="100001",
        country_code="NG",
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))

    assert context["geocode_target"] is not None
    assert context["geocode_target"]["payload"]["address_line1"] == "123 Sample Street"
    assert json.loads(context["geocode_target"]["payload_json"]) == context["geocode_target"]["payload"]
