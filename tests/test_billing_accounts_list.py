from pathlib import Path

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


def test_billing_accounts_template_uses_normal_get_filters():
    template = Path("templates/admin/billing/accounts.html").read_text()

    assert 'method="get" action="/admin/billing/accounts"' in template
    assert 'hx-target="#accounts-table"' not in template
    assert 'name="balance_filter"' not in template
    assert "status_filter" in template


def test_billing_accounts_template_has_no_dead_deactivate_or_delete_actions():
    template = Path("templates/admin/billing/accounts.html").read_text()

    assert 'hx-post="/admin/billing/accounts/' not in template
    assert 'hx-delete="/admin/billing/accounts/' not in template
    assert "/deactivate" not in template
    assert "Delete Account" not in template
