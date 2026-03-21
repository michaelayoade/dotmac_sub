from decimal import Decimal

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.models.subscriber import Subscriber
from app.services.web_customer_details import build_organization_detail_snapshot, build_person_detail_snapshot


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


def test_organization_detail_marks_mixed_billing_policy(db_session, subscriber):
    db_session.add_all(
        [
            _billing_setting("billing_enabled", "true"),
            _billing_setting("payment_due_days", "14"),
        ]
    )
    org_subscriber = Subscriber(
        first_name="Org",
        last_name="Member",
        email="org-member@example.com",
        organization_id=subscriber.organization_id,
    )
    subscriber.payment_due_days = 7
    subscriber.organization_id = subscriber.organization_id or None
    db_session.add(org_subscriber)
    db_session.flush()

    from app.models.subscriber import Organization

    organization = Organization(name="Test Org")
    db_session.add(organization)
    db_session.flush()
    subscriber.organization_id = organization.id
    org_subscriber.organization_id = organization.id
    org_subscriber.payment_due_days = 21
    db_session.commit()

    context = build_organization_detail_snapshot(db_session, str(organization.id))
    rows = {row["key"]: row for row in context["billing_policy"]["rows"]}

    assert context["billing_policy"]["has_mixed"] is True
    assert rows["payment_due_days"]["effective"] == "Mixed"
    assert rows["payment_due_days"]["source"] == "Mixed"
