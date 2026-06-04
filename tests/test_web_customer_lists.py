from app.models.subscriber import Subscriber, UserType
from app.services.web_customer_lists import build_customers_index_context


def test_customer_list_excludes_reseller_users(db_session):
    customer = Subscriber(
        first_name="Customer",
        last_name="User",
        email="customer-list@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    reseller = Subscriber(
        first_name="Reseller",
        last_name="User",
        email="reseller-list@example.com",
        user_type=UserType.reseller,
        is_active=True,
    )
    db_session.add_all([customer, reseller])
    db_session.commit()

    context = build_customers_index_context(
        db_session,
        search=None,
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert customer.email in emails
    assert reseller.email not in emails
