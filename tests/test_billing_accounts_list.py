from decimal import Decimal
from pathlib import Path

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services import web_billing_accounts


def test_billing_accounts_list_applies_search_and_status_filters(db_session):
    alpha = Subscriber(
        first_name="Alpha",
        last_name="Customer",
        email="alpha@example.com",
        user_type=UserType.customer,
        subscriber_number="ALPHA-001",
        status=SubscriberStatus.active,
    )
    beta = Subscriber(
        first_name="Beta",
        last_name="Customer",
        email="beta@example.com",
        user_type=UserType.customer,
        subscriber_number="BETA-001",
        status=SubscriberStatus.blocked,
    )
    db_session.add_all([alpha, beta])
    db_session.commit()

    state = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        search="BETA",
        status="blocked",
    )

    assert state["accounts"] == [beta]
    assert state["total"] == 1
    assert state["search"] == "BETA"
    assert state["status_filter"] == "blocked"


def test_billing_accounts_list_applies_open_balance_filter(db_session):
    due = Subscriber(
        first_name="Due",
        last_name="Customer",
        email="due@example.com",
        user_type=UserType.customer,
        subscriber_number="DUE-001",
        status=SubscriberStatus.active,
    )
    credit = Subscriber(
        first_name="Credit",
        last_name="Customer",
        email="credit@example.com",
        user_type=UserType.customer,
        subscriber_number="CREDIT-001",
        status=SubscriberStatus.active,
    )
    zero = Subscriber(
        first_name="Zero",
        last_name="Customer",
        email="zero@example.com",
        user_type=UserType.customer,
        subscriber_number="ZERO-001",
        status=SubscriberStatus.active,
    )
    db_session.add_all([due, credit, zero])
    db_session.flush()
    db_session.add_all(
        [
            Invoice(
                account_id=due.id,
                status=InvoiceStatus.issued,
                currency="NGN",
                total=Decimal("125.00"),
                balance_due=Decimal("125.00"),
            ),
            Invoice(
                account_id=due.id,
                status=InvoiceStatus.draft,
                currency="NGN",
                total=Decimal("999.00"),
                balance_due=Decimal("999.00"),
            ),
            Invoice(
                account_id=credit.id,
                status=InvoiceStatus.issued,
                currency="NGN",
                total=Decimal("-25.00"),
                balance_due=Decimal("-25.00"),
            ),
        ]
    )
    db_session.commit()

    positive = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        balance_filter="positive",
    )
    assert positive["accounts"] == [due]
    assert positive["accounts"][0].balance == Decimal("125.00")
    assert positive["total_balance"] == 125.0

    credit_state = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        balance_filter="credit",
    )
    assert credit_state["accounts"] == [credit]
    assert credit_state["accounts"][0].balance == Decimal("-25.00")

    zero_state = web_billing_accounts.build_accounts_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        balance_filter="zero",
    )
    assert zero_state["accounts"] == [zero]
    assert zero_state["balance_filter"] == "zero"


def test_billing_accounts_template_uses_normal_get_filters():
    template = Path("templates/admin/billing/accounts.html").read_text()

    assert 'method="get" action="/admin/billing/accounts"' in template
    assert 'hx-target="#accounts-table"' not in template
    assert 'name="balance_filter"' in template
    assert "status_filter" in template


def test_billing_accounts_template_has_no_dead_deactivate_or_delete_actions():
    template = Path("templates/admin/billing/accounts.html").read_text()

    assert 'hx-post="/admin/billing/accounts/' not in template
    assert 'hx-delete="/admin/billing/accounts/' not in template
    assert "/deactivate" not in template
    assert "Delete Account" not in template
