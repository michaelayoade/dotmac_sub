from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from app.services import reseller_onboarding
from app.services.web_admin_resellers import ResellerCatalogOfferOption
from app.web.admin.resellers import templates

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates/admin/resellers/detail.html"
).read_text()

RESELLER_ID = UUID("6017d1ca-8c53-4bc2-a969-5b7f769f142a")


class _State:
    csrf_token = "test-csrf-token"
    auth: dict[str, object] = {"permission_keys": {"reseller:write"}}


class _Request:
    state = _State()
    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    session: dict[str, object] = {}
    client = None
    scope: dict[str, object] = {}
    url = SimpleNamespace(path=f"/admin/resellers/{RESELLER_ID}")

    def url_for(self, *args: object, **kwargs: object) -> str:
        return "/"


def _render_detail() -> str:
    reseller = SimpleNamespace(
        id=RESELLER_ID,
        name="Example Reseller",
        code="EXAMPLE",
        contact_email="partner@example.com",
        contact_phone="08000000000",
        policy_set_id=None,
        is_active=True,
    )
    urls = SimpleNamespace(
        accounts="#accounts",
        billing_overview="#billing",
        catalog="#catalog-access",
        invoices="#invoices",
        payments="#payments",
        provisioning="#provisioning",
        services="#services",
        subscribers="#subscribers",
    )
    return templates.env.get_template("admin/resellers/detail.html").render(
        request=_Request(),
        active_page="resellers",
        active_menu="customers",
        current_user={"name": "Test Admin", "email": "admin@example.com"},
        sidebar_stats={},
        reseller=reseller,
        reseller_urls=urls,
        reseller_subscribers_total=0,
        subscriber_status_counts={},
        outstanding_balance_by_currency=[],
        overdue_invoices=0,
        payments_30d_count=0,
        active_services=0,
        pending_services=0,
        suspended_services=0,
        subscriptions_total=0,
        open_tickets=0,
        reseller_portal_users=0,
        reseller_portal_user_views=[],
        explicit_available_offers_total=1,
        reseller_subscribers=[],
        recent_invoices=[],
        recent_payments=[],
        recent_subscriptions=[],
        recent_tickets=[],
        explicit_available_offers=[],
        reseller_catalog_offer_options=[
            ResellerCatalogOfferOption(
                offer_id=UUID("6e9c6427-c9f6-4c32-9f4f-a724b63cadf0"),
                name="Unlimited Plus",
                code="UNLIMITED-PLUS",
                plan_family="unlimited",
                assigned_to_reseller=True,
                active_assignment_count=2,
            )
        ],
        policy_sets=[],
        roles=[],
        error=None,
        notice=None,
        portal_invite_policy=reseller_onboarding.ResellerPortalInvitePolicy(
            principal_type=reseller_onboarding.ResellerPortalPrincipalType.RESELLER_USER,
            subscriber_role_assignment_supported=False,
        ),
    )


def test_link_customer_form_is_exposed_from_linked_subscribers_modal() -> None:
    linked_subscribers_heading = TEMPLATE.index("Linked Subscribers")
    link_customer_trigger = TEMPLATE.index("Link a customer")
    subscribers_table = TEMPLATE.index('<table class="w-full text-sm">')

    assert linked_subscribers_heading < link_customer_trigger < subscribers_table
    assert "showLinkCustomerModal: false" in TEMPLATE
    assert 'id="link-customer-modal"' in TEMPLATE
    assert 'role="dialog"' in TEMPLATE
    assert 'aria-modal="true"' in TEMPLATE
    assert TEMPLATE.count('action="/admin/resellers/{{ reseller.id }}/users/link"') == 1


def test_invite_user_form_is_exposed_from_reseller_details_header() -> None:
    reseller_details_heading = TEMPLATE.index("Reseller Details")
    invite_user_trigger = TEMPLATE.index("Invite user")
    reseller_details_list = TEMPLATE.index('<dl class="mt-4 space-y-3 text-sm">')

    assert reseller_details_heading < invite_user_trigger < reseller_details_list
    assert "showInviteUserModal: false" in TEMPLATE
    assert 'id="invite-reseller-user-modal"' in TEMPLATE
    assert 'aria-labelledby="invite-reseller-user-modal-title"' in TEMPLATE
    assert (
        TEMPLATE.count('action="/admin/resellers/{{ reseller.id }}/users/create"') == 1
    )
    assert 'name="username"' in TEMPLATE
    assert "The email may be shared with a customer." in TEMPLATE


def test_reseller_detail_exposes_portal_access_evidence() -> None:
    assert "Portal access" in TEMPLATE
    assert "reseller_portal_user_views" in TEMPLATE
    assert "Invite pending" in TEMPLATE


def test_first_class_reseller_invite_does_not_offer_subscriber_roles() -> None:
    template = templates.env.get_template("admin/resellers/_invite_role_field.html")

    html = template.render(
        portal_invite_policy=reseller_onboarding.ResellerPortalInvitePolicy(
            principal_type=reseller_onboarding.ResellerPortalPrincipalType.RESELLER_USER,
            subscriber_role_assignment_supported=False,
        )
    )

    assert 'name="role"' not in html
    assert "Subscriber roles do not apply to reseller portal users." in html


def test_legacy_reseller_invite_offers_subscriber_roles() -> None:
    template = templates.env.get_template("admin/resellers/_invite_role_field.html")

    html = template.render(
        portal_invite_policy=reseller_onboarding.ResellerPortalInvitePolicy(
            principal_type=reseller_onboarding.ResellerPortalPrincipalType.SUBSCRIBER,
            subscriber_role_assignment_supported=True,
        ),
        roles=[{"name": "reseller-admin"}],
    )

    assert 'name="role"' in html
    assert "reseller-admin" in html


def test_catalog_access_management_renders_as_distinct_admin_control() -> None:
    html = _render_detail()

    assert f'action="/admin/resellers/{RESELLER_ID}/catalog-access"' in html
    assert 'name="offer_ids"' in html
    assert 'value="6e9c6427-c9f6-4c32-9f4f-a724b63cadf0"' in html
    assert "Unlimited Plus" in html
    assert "Restricted to 2 resellers; assigned here." in html
    assert "Plans Available for Changing Services" in html
    assert "does not control customer self-service changes" in html
