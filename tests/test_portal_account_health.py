from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.templating import Jinja2Templates
from sqlalchemy import event

from app.api import me as me_api
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode, SubscriptionStatus
from app.schemas.portal_account_health import PortalAccountHealthRead
from app.services import portal_account_health
from app.services.portal_account_health import build_portal_account_health


def _invoice(db_session, account_id, *, currency: str, balance: str) -> Invoice:
    invoice = Invoice(
        account_id=account_id,
        invoice_number=f"PORTAL-{uuid.uuid4().hex[:8]}",
        status=InvoiceStatus.issued,
        total=Decimal(balance),
        balance_due=Decimal(balance),
        currency=currency,
        is_active=True,
        is_proforma=False,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_portal_account_health_keeps_currency_lanes_and_funding_distinct(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    _invoice(db_session, subscriber_account.id, currency="NGN", balance="100.00")
    _invoice(db_session, subscriber_account.id, currency="USD", balance="5.00")

    health = build_portal_account_health(db_session, subscriber_account.id)

    lanes = health.financial.receivables.value
    assert [(lane.currency, lane.outstanding) for lane in lanes] == [
        ("NGN", Decimal("100.00")),
        ("USD", Decimal("5.00")),
    ]
    assert health.financial.prepaid_funding.kind.value == "not_applicable"
    assert len(health.services) == 1
    assert health.services[0].subscription_id == subscription.id
    wire = PortalAccountHealthRead.model_validate(health)
    assert wire.services[0].subscription_id == subscription.id
    assert [lane.currency for lane in wire.financial.receivables.value or []] == [
        "NGN",
        "USD",
    ]


def test_portal_account_health_marks_receivables_unavailable_not_zero(
    db_session, subscriber_account, monkeypatch
):
    subscriber_account.billing_mode = BillingMode.postpaid
    db_session.commit()

    def _unavailable(*_args, **_kwargs):
        raise RuntimeError("financial projection unavailable")

    monkeypatch.setattr(
        portal_account_health,
        "get_customer_receivable_summaries",
        _unavailable,
    )

    health = build_portal_account_health(db_session, subscriber_account.id)

    assert health.financial.receivables.kind.value == "unavailable"
    assert health.financial.receivables.value is None
    assert health.has_partial_data is True


def test_portal_account_health_has_an_explicit_single_service_query_budget(
    db_session, subscriber_account, subscription
):
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    statements = 0

    def _count(*_args, **_kwargs):
        nonlocal statements
        statements += 1

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", _count)
    try:
        health = build_portal_account_health(db_session, subscriber_account.id)
    finally:
        event.remove(bind, "before_cursor_execute", _count)

    assert len(health.services) == 1
    assert statements <= 28, f"Account Health used {statements} SQL statements"


def test_portal_templates_share_owner_projection_and_remove_generic_balance():
    root = Path(__file__).resolve().parents[1]
    customer = (root / "templates/customer/dashboard/index.html").read_text()
    reseller = (root / "templates/reseller/accounts/detail.html").read_text()
    shared = (root / "templates/components/portal/account_health.html").read_text()

    assert "account_health" in customer
    assert "account_health" in reseller
    assert "financial_health" in customer and "service_health_strip" in customer
    assert "financial_health" in reseller and "service_health_strip" in reseller
    assert "account.balance" not in customer
    assert "account.open_balance" not in reseller
    assert "status_lower" not in reseller
    assert "Current Balance" not in customer
    assert "Outstanding receivables" in shared
    assert "Prepaid service funding" in shared


def test_portal_account_health_templates_compile():
    env = Jinja2Templates(directory="templates").env
    env.filters["money"] = str
    env.filters["portal_datetime"] = str
    env.get_template("components/portal/account_health.html")
    env.get_template("customer/dashboard/index.html")
    env.get_template("customer/services/detail.html")
    env.get_template("reseller/accounts/detail.html")


def test_mobile_is_cut_over_to_the_shared_account_health_contract():
    root = Path(__file__).resolve().parents[1]
    me_api = (root / "app/api/me.py").read_text()
    mobile_model = (root / "mobile/lib/src/models/account_health.dart").read_text()

    assert "build_portal_account_health" in me_api
    assert '@router.get("/account-health"' in me_api
    assert '@router.get("/service-status"' not in me_api
    assert '@router.get("/connection-status"' not in me_api
    assert "class AccountHealth" in mobile_model
    assert "primaryAction" in mobile_model


def test_account_health_api_is_self_scoped(db_session, subscriber_account):
    result = me_api.my_account_health(
        db=db_session,
        principal={
            "principal_type": "subscriber",
            "subscriber_id": str(subscriber_account.id),
        },
    )
    assert result.account_id == subscriber_account.id

    with pytest.raises(HTTPException) as exc:
        me_api.my_account_health(
            db=db_session,
            principal={
                "principal_type": "system_user",
                "subscriber_id": str(subscriber_account.id),
            },
        )
    assert exc.value.status_code == 403
