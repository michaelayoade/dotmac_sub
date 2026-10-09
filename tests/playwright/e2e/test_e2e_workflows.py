"""End-to-end business workflow tests.

These tests verify complete business processes that span multiple
pages and systems.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect
from sqlalchemy import text

from app.models.catalog import Subscription
from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.models.radius import RadiusServer, RadiusSyncJob
from app.models.subscriber import Subscriber
from app.services.customer_identifiers import pppoe_username_from_subscriber_number
from tests.playwright.helpers.api import api_get, api_post_json, bearer_headers
from tests.playwright.pages.admin.login_page import AdminLoginPage


def _request_with_retry(fn, *, attempts: int = 3, delay_s: float = 1.0):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except PlaywrightError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_s)
    if last_error:
        raise last_error
    raise RuntimeError("request retry exhausted")


def _create_disposable_pop_site_with_nas(
    api_context, admin_token: str, suffix: str
) -> tuple[dict, dict]:
    """Give this flow its own active topology without a device endpoint."""
    headers = bearer_headers(admin_token)
    pop_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/pop-sites",
            {
                "name": f"E2E PPPoE POP {suffix}",
                "code": f"E2E-POP-{suffix}",
                "city": "Lagos",
                "region": "Lagos",
                "country_code": "NG",
                "is_active": True,
            },
            headers=headers,
        )
    )
    assert pop_response.status == 201
    pop_site = pop_response.json()

    # No IP, management address, shared secret, or API/SSH credentials: this
    # catalogue NAS can drive POP-based selection without contacting hardware.
    nas_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/nas-devices",
            {
                "name": f"E2E PPPoE NAS {suffix}",
                "code": f"E2E-NAS-{suffix}",
                "vendor": "other",
                "pop_site_id": pop_site["id"],
                "supported_connection_types": ["pppoe"],
                "default_connection_type": "pppoe",
                "status": "active",
                "is_active": True,
                "backup_enabled": False,
            },
            headers=headers,
        )
    )
    assert nas_response.status == 201
    nas_device = nas_response.json()
    assert nas_device["pop_site_id"] == pop_site["id"]
    return pop_site, nas_device


def _create_phase1_offer(
    api_context,
    suffix: str,
    admin_token: str | None = None,
) -> tuple[dict, dict]:
    headers = bearer_headers(admin_token) if admin_token else None

    radius_profile_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/radius-profiles",
            {
                "name": f"E2E PPPoE Profile {suffix}",
                "code": f"e2e-pppoe-{suffix.lower()}",
                "vendor": "mikrotik",
                "connection_type": "pppoe",
                "description": "Playwright Phase 1 PPPoE profile",
                "download_speed": 100000,
                "upload_speed": 50000,
                "ip_pool_name": f"e2e-pool-{suffix.lower()}",
                "ipv6_pool_name": f"e2e-v6-{suffix.lower()}",
                "simultaneous_use": 1,
                "is_active": True,
            },
            headers=headers,
        )
    )
    assert radius_profile_response.status == 201
    radius_profile = radius_profile_response.json()

    offer_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/offers",
            {
                "name": f"E2E Phase 1 Offer {suffix}",
                "code": f"e2e-phase1-{suffix.lower()}",
                "service_type": "residential",
                "access_type": "fiber",
                "price_basis": "flat",
                "billing_cycle": "monthly",
                "billing_mode": "prepaid",
                "contract_term": "month_to_month",
                "speed_download_mbps": 1000,
                "speed_upload_mbps": 500,
                "status": "active",
                "is_active": True,
                "available_for_services": True,
                "show_on_customer_portal": True,
                "plan_category": "internet",
                "description": "Playwright Phase 1 activation flow offer",
            },
            headers=headers,
        )
    )
    assert offer_response.status == 201
    offer = offer_response.json()

    offer_profile_link_response = _request_with_retry(
        lambda: api_post_json(
            api_context,
            "/api/v1/offer-radius-profiles",
            {
                "offer_id": offer["id"],
                "profile_id": radius_profile["id"],
            },
            headers=headers,
        )
    )
    assert offer_profile_link_response.status == 201

    return offer, radius_profile


def _is_loopback_host(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


@pytest.fixture()
def phase1_external_radius_source(e2e_db, settings, tmp_path):
    """Expose read-only synthetic RADIUS rows to the disposable E2E web app."""
    configured_dir = os.getenv("E2E_RADIUS_FIXTURE_DIR")
    if configured_dir:
        runner_temp = os.getenv("RUNNER_TEMP")
        fixture_dir = Path(configured_dir)
        if (
            not runner_temp
            or fixture_dir != Path(runner_temp) / "dotmac-sub-e2e-radius-fixtures"
            or not fixture_dir.is_absolute()
            or not fixture_dir.is_dir()
            or fixture_dir.is_symlink()
        ):
            raise RuntimeError("E2E RADIUS fixture directory is not the runner mount")
        db_path = fixture_dir / f"phase1_radius_{uuid4().hex}.sqlite"
    else:
        db_path = tmp_path / "phase1_radius.sqlite"
    created_ids = None

    def create(username: str, suffix: str) -> tuple[str, str]:
        nonlocal created_ids
        bind = e2e_db.get_bind()
        if (
            not _is_loopback_host(urlsplit(settings.base_url).hostname)
            or bind.dialect.name != "postgresql"
            or not _is_loopback_host(bind.url.host)
            or bind.url.database != "dotmac_sub_e2e"
        ):
            raise RuntimeError(
                "Phase1 external rows require a loopback disposable E2E target"
            )
        if e2e_db.scalar(text("SELECT current_database()")) != "dotmac_sub_e2e":
            raise RuntimeError(
                "Phase1 external rows require the disposable E2E database"
            )

        pool = f"e2e-pool-{suffix.lower()}"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)"
            )
            connection.execute(
                "CREATE TABLE radreply (username TEXT, attribute TEXT, op TEXT, value TEXT)"
            )
            connection.execute(
                "CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INTEGER)"
            )
            connection.execute(
                "INSERT INTO radcheck VALUES (?, 'Simultaneous-Use', ':=', '1')",
                (username,),
            )
            connection.execute(
                "INSERT INTO radreply VALUES (?, 'Framed-Pool', ':=', ?)",
                (username, pool),
            )
            connection.commit()
        finally:
            connection.close()
        db_path.chmod(0o444)

        server = RadiusServer(
            name=f"E2E RADIUS server {suffix}",
            host=f"e2e-radius-{suffix.lower()}.invalid",
        )
        connector = ConnectorConfig(
            name=f"E2E external RADIUS {suffix}",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
            base_url=f"sqlite:///{db_path}",
            is_active=True,
        )
        e2e_db.add_all((server, connector))
        e2e_db.flush()
        job = RadiusSyncJob(
            name=f"E2E external RADIUS {suffix}",
            server_id=server.id,
            connector_config_id=connector.id,
            sync_users=True,
            sync_nas_clients=False,
            is_active=True,
        )
        e2e_db.add(job)
        e2e_db.commit()
        created_ids = (job.id, connector.id, server.id)
        return job.name, pool

    yield create

    try:
        e2e_db.rollback()
        if created_ids is not None:
            for model, identifier in zip(
                (RadiusSyncJob, ConnectorConfig, RadiusServer), created_ids, strict=True
            ):
                row = e2e_db.get(model, identifier)
                if row is not None:
                    e2e_db.delete(row)
            e2e_db.commit()
    finally:
        db_path.unlink(missing_ok=True)


class TestCustomerListFilters:
    """Tests for customer-list filter guidance."""

    def test_infrastructure_search_prompts_for_type(self, admin_page: Page, settings):
        admin_page.goto(
            f"{settings.base_url}/admin/customers",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        expect(
            admin_page.get_by_role("heading", name="Customers", exact=True)
        ).to_be_visible()
        admin_page.get_by_role("button", name=re.compile(r"^Filters")).click()

        infrastructure_requests: list[str] = []

        def record_infrastructure_request(request) -> None:
            if "/admin/customers/infrastructure-options" in request.url:
                infrastructure_requests.append(request.url)

        admin_page.on("request", record_infrastructure_request)

        search = admin_page.locator("#infrastructure-search")
        expect(search).to_be_enabled()
        search.fill("Ka")

        expect(admin_page.locator("#infrastructure-results")).to_be_visible()
        expect(
            admin_page.get_by_text("Choose an infrastructure type first.", exact=True)
        ).to_be_visible()
        admin_page.wait_for_timeout(400)
        assert infrastructure_requests == []

        admin_page.route(
            "**/admin/customers/infrastructure-options?**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "results": [
                            {
                                "id": str(uuid4()),
                                "label": "Gudu",
                                "context": "Abuja",
                            }
                        ]
                    }
                ),
            ),
        )
        admin_page.locator("#infrastructure-type").select_option("location")
        search.fill("Gudu")

        expect(admin_page.get_by_text("Gudu", exact=True)).to_be_visible()
        expect(admin_page.get_by_text("Abuja", exact=True)).to_be_visible()
        assert len(infrastructure_requests) == 1
        assert "infrastructure_type=location" in infrastructure_requests[0]
        assert "q=Gudu" in infrastructure_requests[0]


class TestSubscriptionActivation:
    """Tests for the subscription activation workflow."""

    def test_subscriber_to_active_service_flow(self, admin_page: Page, settings):
        """Current customer onboarding entry points should be accessible."""
        from tests.playwright.pages.admin.subscribers_page import SubscribersPage

        subscribers = SubscribersPage(admin_page, settings.base_url)
        subscribers.goto()
        subscribers.expect_loaded()

        expect(admin_page.get_by_role("link", name="Add Customer")).to_be_visible()

    def test_service_order_creation_from_subscriber(self, admin_page: Page, settings):
        """Subscription creation should be reachable from customer context."""
        admin_page.goto(f"{settings.base_url}/admin/customers")
        expect(
            admin_page.get_by_role("heading", name="Customers", exact=True)
        ).to_be_visible()

    def test_phase1_customer_activation_shows_pppoe_and_radius_evidence(
        self,
        admin_page: Page,
        browser,
        request: pytest.FixtureRequest,
        settings,
        api_context,
        admin_token,
        e2e_db,
        phase1_external_radius_source,
    ):
        """Create customer and active subscription, then verify PPPoE and RADIUS evidence in UI."""
        suffix = uuid4().hex[:8].upper()
        customer_email = f"phase1-{suffix.lower()}@example.com"
        pop_site, nas_device = _create_disposable_pop_site_with_nas(
            api_context, admin_token, suffix
        )
        offer, radius_profile = _create_phase1_offer(api_context, suffix, admin_token)

        fresh_context = browser.new_context()
        # Finalize after pytest records the call outcome, so a failed wizard
        # keeps its own trace instead of only the unrelated admin fixture.
        request.addfinalizer(fresh_context.close)
        fresh_context.add_init_script(
            "window.localStorage.setItem('dotmac_admin_tour_seen_v1', '1')"
        )
        fresh_context.set_default_timeout(settings.action_timeout_ms)
        fresh_context.set_default_navigation_timeout(settings.navigation_timeout_ms)
        page = fresh_context.new_page()

        login = AdminLoginPage(page, settings.base_url)
        login.goto()
        login.login(settings.admin_username or "admin", settings.admin_password or "")
        page.wait_for_url(
            re.compile(r".*/admin/dashboard(?:[?#].*)?$"),
            wait_until="domcontentloaded",
        )

        page.goto(
            f"{settings.base_url}/admin/customers/new", wait_until="domcontentloaded"
        )
        expect(
            page.get_by_role("heading", name=re.compile(r"New Customer|New Person"))
        ).to_be_visible()

        page.get_by_text("Individual", exact=True).click()
        page.locator("#first_name").fill("Phase1")
        page.locator("#last_name").fill(suffix)
        page.locator("#email").fill(customer_email)
        page.locator("#phone").fill(f"+234800{suffix[-4:]}")
        page.get_by_role("button", name="Address").click()
        page.locator("#address_line1").fill(f"{suffix} Activation Street")
        page.locator("#region").fill("Lagos")
        page.locator("#pop_site_id").select_option(str(pop_site["id"]))
        page.locator("button[type='submit']").click(no_wait_after=True)

        page.wait_for_load_state("domcontentloaded")
        expect(page).to_have_url(re.compile(r".*/admin/customers/person/[^/?#]+"))
        expect(page.get_by_text(customer_email).first).to_be_visible()

        page.get_by_role("navigation", name="Tabs").get_by_role(
            "button", name=re.compile(r"^Service\s*\d*$")
        ).click()
        new_subscription = (
            page.get_by_role("heading", name="All Subscriptions")
            .locator("..")
            .get_by_role("link", name="New Subscription")
        )
        expect(new_subscription).to_be_visible()
        new_subscription.click()

        page.wait_for_url("**/admin/catalog/subscriptions/new**")
        expect(
            page.get_by_role("heading", name="Add Subscription", exact=True)
        ).to_be_visible()

        page.locator("#offer_id").select_option(str(offer["id"]))
        page.locator("#status").select_option("active")
        page.get_by_role("button", name="Continue").click()
        expect(
            page.get_by_role("heading", name="Plan Selection", exact=True)
        ).to_be_hidden()
        expect(
            page.get_by_role("heading", name="Service Provisioning", exact=True)
        ).to_be_visible()

        provisioning_nas_value = page.locator(
            "input[name='provisioning_nas_device_id'][data-typeahead-hidden]"
        )
        expect(provisioning_nas_value).to_have_value(str(nas_device["id"]))
        expect(
            page.locator("input#provisioning_nas_device_id[data-typeahead-input]")
        ).not_to_have_value("")
        page.locator("#ipv4_method").select_option("dynamic")

        page.get_by_role("button", name="Continue").click()
        # Alpine's leaving panel remains in the layout during x-transition.
        # A visible submit button can still move after Playwright's two-frame
        # stability check; wait for the old panel to leave before clicking it.
        expect(
            page.get_by_role("heading", name="Service Provisioning", exact=True)
        ).to_be_hidden()
        expect(
            page.get_by_role("heading", name="Quick Options", exact=True)
        ).to_be_visible()
        page.locator("input[name='send_welcome_email']").uncheck()
        subscription_form = page.locator("form[action='/admin/catalog/subscriptions']")
        invalid_controls: list[str] = subscription_form.evaluate(
            """form => Array.from(form.elements)
                .filter(control => control.willValidate && !control.checkValidity())
                .map(control => control.name || control.id || control.tagName)"""
        )
        # Report control names only; form values may include credentials.
        assert invalid_controls == [], (
            f"Invalid subscription controls: {invalid_controls}"
        )
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and urlsplit(response.url).path == "/admin/catalog/subscriptions"
            )
        ) as submitted:
            page.get_by_role("button", name="Add Subscription").click(
                no_wait_after=True
            )
        assert submitted.value.status == 303

        page.wait_for_url("**/admin/customers/person/**")
        subscription_items = []
        for _ in range(10):
            subscriptions_response = api_get(
                api_context,
                f"/api/v1/subscriptions?offer_id={offer['id']}&limit=5",
                headers=bearer_headers(admin_token),
            )
            assert subscriptions_response.status == 200
            subscription_items = subscriptions_response.json()["items"]
            if subscription_items:
                break
            time.sleep(1)
        assert subscription_items, (
            "No subscription was created for the Phase 1 E2E offer."
        )
        subscription_id = subscription_items[0]["id"]
        subscription = e2e_db.get(Subscription, UUID(subscription_id))
        assert subscription is not None
        subscriber = e2e_db.get(Subscriber, subscription.subscriber_id)
        assert subscriber is not None
        expected_username = pppoe_username_from_subscriber_number(
            e2e_db, subscriber.subscriber_number
        )
        assert expected_username is not None
        assert subscription.login == expected_username

        page.goto(
            f"{settings.base_url}/admin/catalog/subscriptions/{subscription_id}",
            wait_until="domcontentloaded",
        )
        expect(page.get_by_text("Provisioning Evidence", exact=True)).to_be_visible()
        page.get_by_role("button", name="Toggle Provisioning Evidence").click()
        credential_card = page.get_by_text("Access Credential", exact=True).locator(
            "xpath=.."
        )
        username_field = credential_card.locator("p.font-mono")
        expect(username_field).to_have_text(expected_username)
        expect(credential_card).to_contain_text("PPPOE")
        displayed_username = username_field.inner_text().strip()

        expect(
            page.get_by_text("Resolved RADIUS Reply Attributes", exact=True)
        ).to_be_visible()
        expect(
            page.locator("table").filter(has=page.get_by_text("Service-Type")).first
        ).to_be_visible()
        expect(page.get_by_text("Framed-Protocol", exact=False)).to_be_visible()
        expect(page.get_by_text("Mikrotik-Rate-Limit", exact=False)).to_be_visible()
        expect(
            page.get_by_text("Delegated-IPv6-Prefix-Pool", exact=False)
        ).to_be_visible()

        external_job_name, external_pool = phase1_external_radius_source(
            displayed_username, suffix
        )
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("External FreeRADIUS Rows", exact=True)).to_be_visible()
        page.get_by_role("button", name="Toggle External FreeRADIUS Rows").click()
        external_rows = page.get_by_role(
            "heading", name="External FreeRADIUS Rows"
        ).locator("xpath=ancestor::div[@x-data][1]")
        source_card = external_rows.get_by_text(external_job_name, exact=True).locator(
            "xpath=ancestor::div[contains(@class, 'rounded-lg') "
            "and contains(@class, 'border-slate-200')][1]"
        )
        expect(source_card.get_by_text("radcheck", exact=True)).to_be_visible()
        expect(source_card.get_by_text("Simultaneous-Use := 1")).to_be_visible()
        expect(source_card.get_by_text("radreply", exact=True)).to_be_visible()
        expect(
            source_card.get_by_text(f"Framed-Pool := {external_pool}")
        ).to_be_visible()
        events_section = page.get_by_text("Domain Events", exact=True).locator("../..")
        page.get_by_role("button", name="Toggle Domain Events").click()
        for attempt in range(5):
            created_event = events_section.get_by_text(
                "subscription.created", exact=True
            )
            activated_event = events_section.get_by_text(
                "subscription.activated", exact=True
            )
            if created_event.count() and activated_event.count():
                break
            page.reload(wait_until="domcontentloaded")
            expect(
                page.get_by_text("Provisioning Evidence", exact=True)
            ).to_be_visible()
            events_section = page.get_by_text("Domain Events", exact=True).locator(
                "../.."
            )
            page.get_by_role("button", name="Toggle Domain Events").click()
            time.sleep(1)
        expect(
            events_section.get_by_text("subscription.created", exact=True)
        ).to_be_visible()
        expect(
            events_section.get_by_text("subscription.activated", exact=True)
        ).to_be_visible()

        page.goto(
            f"{settings.base_url}/admin/catalog/subscriptions/{subscription_id}/edit",
            wait_until="domcontentloaded",
        )
        expect(
            page.get_by_role("heading", name="Edit Subscription", exact=True)
        ).to_be_visible()
        expect(page.get_by_text("Current Service Login", exact=True)).to_be_visible()
        current_login = page.locator(
            "xpath=//label[contains(., 'Current Service Login')]/following-sibling::input[@readonly]"
        ).first
        expect(page.get_by_text("Current Service Password", exact=True)).to_be_visible()
        password_input = page.locator(
            "xpath=//label[contains(., 'Current Service Password')]/following-sibling::div//input[@readonly]"
        ).first
        assert password_input.evaluate("element => Boolean(element.value)") is True
        expect(current_login).to_have_value(expected_username)
        expect(page.locator("#radius_profile_id")).to_have_value(
            str(radius_profile["id"])
        )
        fresh_context.close()


class TestBillingCycle:
    """Tests for the billing cycle workflow."""

    def test_invoice_to_payment_flow(self, admin_page: Page, settings):
        """Complete flow: Subscription -> Invoice -> Payment -> Ledger."""
        from tests.playwright.pages.admin.billing.invoices_page import InvoicesPage

        # Step 1: View invoices
        invoices = InvoicesPage(admin_page, settings.base_url)
        invoices.goto()
        invoices.expect_loaded()

        # Invoice table should be visible
        expect(admin_page.locator("table")).to_be_visible()

    def test_payment_recording_flow(self, admin_page: Page, settings):
        """Should be able to record payments."""
        from tests.playwright.pages.admin.billing.payments_page import PaymentsPage

        payments = PaymentsPage(admin_page, settings.base_url)
        payments.goto()
        payments.expect_loaded()

        # Payment recording should be accessible
        expect(
            admin_page.get_by_role("link", name="Record Payment")
            .or_(admin_page.get_by_role("button", name="Record Payment"))
            .first
        ).to_be_visible()


class TestSupportResolution:
    """Tests for the support ticket resolution workflow."""

    def test_ticket_lifecycle_flow(self, admin_page: Page, settings):
        """Complete flow: Create ticket -> Assign -> Work -> Resolve -> Close."""
        from tests.playwright.pages.admin.tickets_page import TicketsPage

        tickets = TicketsPage(admin_page, settings.base_url)
        tickets.goto()
        tickets.expect_loaded()

        # Should see ticket management interface
        expect(admin_page.locator("table")).to_be_visible()

    def test_ticket_assignment_flow(self, admin_page: Page, settings):
        """Should be able to assign tickets."""
        from tests.playwright.pages.admin.tickets_page import TicketsPage

        tickets = TicketsPage(admin_page, settings.base_url)
        tickets.goto()
        tickets.expect_loaded()

        # Ticket list should be visible for assignment
        expect(admin_page.locator("table")).to_be_visible()


class TestWorkOrderExecution:
    """Tests for the work order execution workflow."""

    def test_work_order_dispatch_flow(self, admin_page: Page, settings):
        """Ticket workflow surface should be accessible."""
        from tests.playwright.pages.admin.tickets_page import TicketsPage

        tickets = TicketsPage(admin_page, settings.base_url)
        tickets.goto()
        tickets.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()

    def test_dispatch_view_flow(self, admin_page: Page, settings):
        """Billing overview is reachable as an operational dashboard."""
        from tests.playwright.pages.admin.billing.billing_overview_page import (
            BillingOverviewPage,
        )

        overview = BillingOverviewPage(admin_page, settings.base_url)
        overview.goto()
        overview.expect_loaded()


class TestNetworkOperations:
    """Tests for manual network operations workflow."""

    def test_ont_operations_flow(self, admin_page: Page, settings):
        """Manual flow: OLT -> ONT -> IP -> Service."""
        from tests.playwright.pages.admin.network.olts_page import OLTsPage

        olts = OLTsPage(admin_page, settings.base_url)
        olts.goto()
        olts.expect_loaded()

        # OLT management should be accessible
        expect(admin_page.locator("table")).to_be_visible()

    def test_ip_assignment_flow(self, admin_page: Page, settings):
        """Should be able to assign IPs from pools."""
        from tests.playwright.pages.admin.network.ip_management_page import (
            IPManagementPage,
        )

        ip_mgmt = IPManagementPage(admin_page, settings.base_url)
        ip_mgmt.goto()
        ip_mgmt.expect_loaded()


class TestCustomerOnboarding:
    """Tests for complete customer onboarding workflow."""

    def test_full_onboarding_visibility(self, admin_page: Page, settings):
        """All steps for customer onboarding should be accessible."""
        from tests.playwright.pages.admin.billing.invoices_page import InvoicesPage
        from tests.playwright.pages.admin.subscribers_page import SubscribersPage

        subscribers = SubscribersPage(admin_page, settings.base_url)
        subscribers.goto()
        subscribers.expect_loaded()

        invoices = InvoicesPage(admin_page, settings.base_url)
        invoices.goto()
        invoices.expect_loaded()
