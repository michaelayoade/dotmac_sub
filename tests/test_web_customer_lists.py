import uuid

from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    NasDevice,
    NasDeviceStatus,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.network_monitoring import PopSite
from app.models.subscriber import Subscriber, UserType
from app.services.web_customer_lists import build_customers_index_context


def _make_offer(db_session):
    offer = CatalogOffer(
        name=f"Customer List Offer {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    return offer


def _make_customer(db_session, email: str) -> Subscriber:
    customer = Subscriber(
        first_name="Customer",
        last_name=email.split("@", 1)[0],
        email=email,
        user_type=UserType.customer,
        is_active=True,
    )
    db_session.add(customer)
    db_session.flush()
    return customer


def _make_pop_site(db_session, name: str) -> PopSite:
    pop_site = PopSite(name=f"{name} {uuid.uuid4().hex[:8]}", is_active=True)
    db_session.add(pop_site)
    db_session.flush()
    return pop_site


def _make_nas(db_session, name: str, pop_site: PopSite | None = None) -> NasDevice:
    nas = NasDevice(
        name=f"{name} {uuid.uuid4().hex[:8]}",
        status=NasDeviceStatus.active,
        is_active=True,
        pop_site_id=pop_site.id if pop_site else None,
    )
    db_session.add(nas)
    db_session.flush()
    return nas


def _make_subscription(
    db_session,
    customer: Subscriber,
    *,
    status: SubscriptionStatus,
    ipv4_address: str | None = None,
    nas_device: NasDevice | None = None,
    login: str | None = None,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=customer.id,
        offer_id=_make_offer(db_session).id,
        status=status,
        billing_mode=BillingMode.postpaid,
        ipv4_address=ipv4_address,
        provisioning_nas_device_id=nas_device.id if nas_device else None,
        login=login,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _make_ipam_assignment(
    db_session,
    customer: Subscriber,
    subscription: Subscription,
    ip_address: str,
) -> IPAssignment:
    address = IPv4Address(address=ip_address)
    db_session.add(address)
    db_session.flush()
    assignment = IPAssignment(
        subscriber_id=customer.id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    return assignment


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


def test_customer_list_ip_search_matches_exact_current_ipv4_only(db_session):
    current = _make_customer(db_session, "current-ip@example.com")
    current_sub = _make_subscription(
        db_session,
        current,
        status=SubscriptionStatus.active,
        ipv4_address="160.119.126.18",
    )
    _make_ipam_assignment(db_session, current, current_sub, "160.119.126.18")

    suffix = _make_customer(db_session, "suffix-ip@example.com")
    _make_subscription(
        db_session,
        suffix,
        status=SubscriptionStatus.active,
        ipv4_address="160.119.126.180",
    )

    historical = _make_customer(db_session, "historical-ip@example.com")
    _make_subscription(
        db_session,
        historical,
        status=SubscriptionStatus.canceled,
        ipv4_address="160.119.126.18",
    )
    db_session.commit()

    context = build_customers_index_context(
        db_session,
        search="160.119.126.18",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert emails == {"current-ip@example.com"}


def test_customer_list_display_prefers_active_ipam_then_active_subscription(
    db_session,
):
    customer = _make_customer(db_session, "display-ip@example.com")
    active_sub = _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.active,
        ipv4_address="10.0.0.5",
    )
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.canceled,
        ipv4_address="10.0.0.9",
    )
    _make_ipam_assignment(db_session, customer, active_sub, "10.0.0.7")
    db_session.commit()

    context = build_customers_index_context(
        db_session,
        search="display-ip",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    row = next(item for item in context["customers"] if item["email"] == customer.email)
    assert row["ipv4"] == "10.0.0.7"
    assert row["ipv4_label"] == "Current IPAM IPv4"


def test_customer_list_trims_search_before_text_matching(db_session):
    suffix = uuid.uuid4().hex[:8]
    customer = _make_customer(db_session, f"trim-search-{suffix}@example.com")
    customer.first_name = f"TrimName{suffix}"
    customer.phone = f"080{suffix[:8]}"
    customer.account_number = f"ACC-TRIM-{suffix}"
    pppoe_login = f"pppoe-trim-{suffix}"
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.active,
        login=pppoe_login,
    )
    db_session.commit()

    search_terms = [
        customer.first_name,
        customer.email,
        customer.phone,
        customer.account_number,
        pppoe_login,
    ]
    for term in search_terms:
        context = build_customers_index_context(
            db_session,
            search=f"  {term}  ",
            status=None,
            customer_type=None,
            nas_id=None,
            pop_site_id=None,
            page=1,
            per_page=25,
        )

        emails = {item["email"] for item in context["customers"]}
        assert customer.email in emails
        assert context["search"] == term


def test_customer_list_does_not_display_placeholder_ipv4(db_session):
    customer = _make_customer(db_session, "placeholder-ip@example.com")
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.active,
        ipv4_address="0.0.0.0",
    )
    db_session.commit()

    context = build_customers_index_context(
        db_session,
        search="placeholder-ip",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    row = next(item for item in context["customers"] if item["email"] == customer.email)
    assert row["ipv4"] is None
    assert row["ipv4_label"] is None


def test_customer_location_filter_uses_customer_pop_site_not_nas_pop_site(db_session):
    karu_bts = _make_pop_site(db_session, "Karu BTS")
    afr_pop = _make_pop_site(db_session, "AFR")
    afr_nas = _make_nas(db_session, "AFR Access", afr_pop)

    karu_customer = _make_customer(db_session, "karu-location@example.com")
    karu_customer.pop_site_id = karu_bts.id
    _make_subscription(
        db_session,
        karu_customer,
        status=SubscriptionStatus.active,
        nas_device=afr_nas,
    )

    afr_customer = _make_customer(db_session, "afr-location@example.com")
    afr_customer.pop_site_id = afr_pop.id
    _make_subscription(
        db_session,
        afr_customer,
        status=SubscriptionStatus.active,
        nas_device=afr_nas,
    )
    db_session.commit()

    context = build_customers_index_context(
        db_session,
        search=None,
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=str(karu_bts.id),
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert karu_customer.email in emails
    assert afr_customer.email not in emails


def test_customer_nas_filter_still_uses_subscription_nas(db_session):
    karu_bts = _make_pop_site(db_session, "Karu BTS")
    afr_pop = _make_pop_site(db_session, "AFR")
    afr_nas = _make_nas(db_session, "AFR Access", afr_pop)

    karu_customer = _make_customer(db_session, "karu-nas@example.com")
    karu_customer.pop_site_id = karu_bts.id
    _make_subscription(
        db_session,
        karu_customer,
        status=SubscriptionStatus.active,
        nas_device=afr_nas,
    )
    db_session.commit()

    context = build_customers_index_context(
        db_session,
        search=None,
        status=None,
        customer_type=None,
        nas_id=str(afr_nas.id),
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert karu_customer.email in emails
    assert any(str(nas.id) == str(afr_nas.id) for nas in context["nas_options"])
