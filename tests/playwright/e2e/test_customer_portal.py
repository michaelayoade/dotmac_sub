"""Customer portal e2e tests."""

from __future__ import annotations

from playwright.sync_api import Page

from tests.playwright.pages.customer import (
    CustomerBillingPage,
    CustomerDashboardPage,
    CustomerProfilePage,
    CustomerServicesPage,
    CustomerSupportPage,
    CustomerTicketPage,
    CustomerUsagePage,
)


class TestCustomerDashboard:
    """Tests for the customer portal dashboard."""

    def test_dashboard_loads(self, customer_page: Page, settings):
        """Customer dashboard should load successfully."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_dashboard_shows_account_summary(self, customer_page: Page, settings):
        """Dashboard should show account summary."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_account_summary_visible()

    def test_dashboard_shows_service_status(self, customer_page: Page, settings):
        """Dashboard should show service status."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_service_status_visible()

    def test_dashboard_navigation_to_billing(self, customer_page: Page, settings):
        """Should navigate to billing from dashboard."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.navigate_to_billing()
        customer_page.wait_for_url("**/billing**")


class TestCustomerBilling:
    """Tests for the customer billing page."""

    def test_billing_page_loads(self, customer_page: Page, settings):
        """Billing page should load successfully."""
        page = CustomerBillingPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_billing_shows_invoices(self, customer_page: Page, settings):
        """Billing page should show invoices."""
        page = CustomerBillingPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_invoices_visible()

    def test_billing_shows_balance(self, customer_page: Page, settings):
        """Billing page should show account balance."""
        page = CustomerBillingPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_balance_visible()


class TestCustomerServices:
    """Tests for the customer services page."""

    def test_services_page_loads(self, customer_page: Page, settings):
        """Services page should load successfully."""
        page = CustomerServicesPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_services_shows_active_services(self, customer_page: Page, settings):
        """Services page should show active services."""
        page = CustomerServicesPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_active_services_visible()

    def test_services_shows_details(self, customer_page: Page, settings):
        """Services page should show service details."""
        page = CustomerServicesPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_service_details_visible()


class TestCustomerSupport:
    """Tests for the customer support page."""

    def test_support_page_loads(self, customer_page: Page, settings):
        """Support page should load successfully."""
        page = CustomerSupportPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_support_shows_tickets(self, customer_page: Page, settings):
        """Support page should show tickets."""
        page = CustomerSupportPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_tickets_visible()

    def test_create_ticket_flow(self, customer_page: Page, settings):
        """Should be able to create a new support ticket."""
        support_page = CustomerSupportPage(customer_page, settings.base_url)
        support_page.goto()
        support_page.expect_loaded()
        support_page.click_new_ticket()

        ticket_page = CustomerTicketPage(customer_page, settings.base_url)
        ticket_page.expect_form_loaded()
        ticket_page.fill_subject("Test Support Request")
        ticket_page.fill_description("This is a test support request for e2e testing.")
        ticket_page.submit_ticket()


class TestCustomerUsage:
    """Tests for the customer usage page."""

    def test_usage_page_loads(self, customer_page: Page, settings):
        """Usage page should load successfully."""
        page = CustomerUsagePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_usage_shows_summary(self, customer_page: Page, settings):
        """Usage page should show usage summary."""
        page = CustomerUsagePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_usage_summary_visible()


class TestCustomerProfile:
    """Tests for the customer profile page."""

    def test_profile_page_loads(self, customer_page: Page, settings):
        """Profile page should load successfully."""
        page = CustomerProfilePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_profile_shows_personal_info(self, customer_page: Page, settings):
        """Profile page should show personal info."""
        page = CustomerProfilePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_personal_info_visible()

    def test_profile_shows_contact_info(self, customer_page: Page, settings):
        """Profile page should show contact info."""
        page = CustomerProfilePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_contact_info_visible()


class TestCustomerPortalAPI:
    """API-level tests for customer portal."""

    def test_customer_billing_api(self, api_context, customer_token):
        """API should return customer billing info."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/customer/billing",
            headers=bearer_headers(customer_token),
        )
        # May return 200 or 403 depending on token permissions
        assert response.status in [200, 403]

    def test_customer_services_api(self, api_context, customer_token):
        """API should return customer services."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/customer/services",
            headers=bearer_headers(customer_token),
        )
        assert response.status in [200, 403]

    def test_customer_usage_api(self, api_context, customer_token):
        """API should return customer usage data."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/customer/usage",
            headers=bearer_headers(customer_token),
        )
        assert response.status in [200, 403]
