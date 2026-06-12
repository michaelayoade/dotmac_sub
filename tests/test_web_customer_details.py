import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def _bare_request(path: str = "/admin/customers/person/x/pppoe-password") -> Request:
    """Minimal request with empty state (no authenticated user) for unit calls."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 5555),
            "server": ("testserver", 80),
        }
    )


from app.models.catalog import AccessCredential, ConnectionType
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Address, Subscriber, SubscriberCategory, UserType
from app.models.subscription_engine import SettingValueType
from app.services.credential_crypto import encrypt_credential
from app.services.web_customer_details import (
    build_business_detail_snapshot,
    build_customer_detail_snapshot,
    build_person_detail_snapshot,
    reveal_customer_pppoe_password,
)
from app.web.admin import customers as customer_routes


def _billing_setting(key: str, value: str) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.billing,
        key=key,
        value_type=SettingValueType.string,
        value_text=value,
        is_active=True,
    )


def test_person_detail_includes_billing_policy_override_snapshot(
    db_session, subscriber
):
    subscriber.user_type = UserType.customer
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
    subscriber.user_type = UserType.customer
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
    subscriber.user_type = UserType.customer
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
    assert (
        json.loads(context["geocode_target"]["payload_json"])
        == context["geocode_target"]["payload"]
    )


def test_person_detail_exposes_pppoe_access_login(db_session, subscriber):
    subscriber.user_type = UserType.customer
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100025929",
        secret_hash=encrypt_credential("hYNAtwqj"),
        connection_type=ConnectionType.pppoe,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    context = build_person_detail_snapshot(db_session, str(subscriber.id))

    assert context["pppoe_access"] == {
        "has_credential": True,
        "credential_id": str(credential.id),
        "login": "100025929",
        "has_password": True,
    }


def test_reveal_customer_pppoe_password_is_customer_scoped(db_session, subscriber):
    subscriber.user_type = UserType.customer
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100025929",
        secret_hash=encrypt_credential("hYNAtwqj"),
        connection_type=ConnectionType.pppoe,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    password, found = reveal_customer_pppoe_password(
        db_session,
        str(subscriber.id),
        credential_id=str(credential.id),
    )
    missing_password, missing_found = reveal_customer_pppoe_password(
        db_session,
        str(subscriber.id),
        credential_id="00000000-0000-0000-0000-000000000000",
    )
    request = _bare_request()
    response = customer_routes.person_pppoe_password(
        request=request,
        customer_id=str(subscriber.id),
        credential_id=str(credential.id),
        db=db_session,
    )

    assert (password, found) == ("hYNAtwqj", True)
    assert (missing_password, missing_found) == ("", False)
    assert response.status_code == 200
    assert json.loads(response.body)["password"] == "hYNAtwqj"

    # The reveal must be audited (who revealed which customer's credential).
    from app.models.audit import AuditEvent

    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer.pppoe_password_reveal")
        .filter(AuditEvent.entity_id == str(subscriber.id))
        .first()
    )
    assert audit is not None
    assert audit.is_success is True


def test_customer_detail_rejects_reseller_users(db_session):
    reseller_user = Subscriber(
        first_name="Mimi",
        last_name="David",
        email="reseller-detail@example.com",
        user_type=UserType.reseller,
        is_active=True,
    )
    db_session.add(reseller_user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        build_customer_detail_snapshot(db_session, str(reseller_user.id))

    assert exc.value.status_code == 404


def test_normalize_usage_period_tolerates_trailing_punctuation():
    assert customer_routes._normalize_usage_period("last,") == "last"
    assert customer_routes._normalize_usage_period("CURRENT!") == "current"
    assert customer_routes._normalize_usage_period("unexpected") == "current"


def test_person_detail_normalizes_usage_period(monkeypatch, db_session):
    captured: dict[str, object] = {}

    def _template_response(template_name, context, status_code=200):
        captured["template_name"] = template_name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(
            template_name=template_name,
            context=context,
            status_code=status_code,
        )

    monkeypatch.setattr(
        customer_routes,
        "templates",
        SimpleNamespace(TemplateResponse=_template_response),
    )
    monkeypatch.setattr(
        customer_routes.web_customer_details_service,
        "build_customer_detail_snapshot",
        lambda db, customer_id: {"customer": SimpleNamespace(id=customer_id)},
    )

    import app.web.admin as admin_module

    monkeypatch.setattr(admin_module, "get_current_user", lambda request: None)
    monkeypatch.setattr(admin_module, "get_sidebar_stats", lambda db: {})

    response = customer_routes.person_detail(
        request=SimpleNamespace(headers={}),
        customer_id="cust-123",
        usage_period="last,",
        usage_page=2,
        usage_per_page=50,
        db=db_session,
    )

    assert response.status_code == 200
    assert captured["template_name"] == "admin/customers/detail.html"
    assert captured["context"]["pppoe_access"] == {
        "has_credential": False,
        "credential_id": None,
        "login": None,
        "has_password": False,
    }
    assert captured["context"]["usage_period"] == "last"


def test_person_detail_stats_normalizes_usage_period(monkeypatch, db_session):
    captured: dict[str, object] = {}

    def _template_response(template_name, context, status_code=200):
        captured["template_name"] = template_name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(
            template_name=template_name,
            context=context,
            status_code=status_code,
        )

    def _get_usage_page(
        db,
        usage_customer,
        *,
        period,
        page,
        per_page,
        allow_postgres_fallback,
    ):
        captured["period"] = period
        return {
            "usage_records": [],
            "period": period,
            "page": page,
            "per_page": per_page,
            "total": 0,
            "total_pages": 1,
            "usage_summary": {},
            "fup_status": None,
            "usage_source": "none",
            "has_subscription": False,
        }

    monkeypatch.setattr(
        customer_routes,
        "templates",
        SimpleNamespace(TemplateResponse=_template_response),
    )
    monkeypatch.setattr(
        customer_routes,
        "_get_subscriber",
        lambda db, subscriber_id: SimpleNamespace(id=subscriber_id),
    )
    monkeypatch.setattr(
        customer_routes.customer_portal,
        "get_usage_page",
        _get_usage_page,
    )
    monkeypatch.setattr(
        customer_routes,
        "resolve_customer_subscription",
        lambda db, usage_customer: SimpleNamespace(id="sub-123"),
    )
    monkeypatch.setattr(
        customer_routes,
        "_load_initial_bandwidth_stats",
        lambda db, subscription_id: {"current_rx_formatted": "1.20 Mbps"},
    )

    response = customer_routes.person_detail_stats(
        request=SimpleNamespace(headers={}),
        customer_id="cust-123",
        usage_period="last,",
        usage_page=1,
        usage_per_page=25,
        db=db_session,
    )

    assert response.status_code == 200
    assert captured["period"] == "last"
    assert captured["template_name"] == "admin/customers/_stats_panel.html"
    assert captured["context"]["usage_portal"]["period"] == "last"
    assert captured["context"]["usage_subscription_id"] == "sub-123"
    assert captured["context"]["bandwidth_chart_initial_stats"] == {
        "current_rx_formatted": "1.20 Mbps"
    }
