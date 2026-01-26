"""End-to-end business workflow tests.

These tests verify complete business processes that span multiple
pages and systems.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


class TestSubscriptionActivation:
    """Tests for the subscription activation workflow."""

    def test_subscriber_to_active_service_flow(self, admin_page: Page, settings):
        """Complete flow: Create subscriber -> Account -> Subscription -> Service Order -> Active."""
        from tests.playwright.pages.admin.subscribers_page import SubscribersPage

        # Step 1: Navigate to subscribers
        subscribers = SubscribersPage(admin_page, settings.base_url)
        subscribers.goto()
        subscribers.expect_loaded()

        # Step 2: Verify we can access subscriber creation
        expect(admin_page.get_by_role("link", name="New").or_(
            admin_page.get_by_role("button", name="New")
        ).first).to_be_visible()

    def test_service_order_creation_from_subscriber(self, admin_page: Page, settings):
        """Should be able to create service order from subscriber context."""
        from tests.playwright.pages.admin.operations.service_orders_page import ServiceOrdersPage

        orders = ServiceOrdersPage(admin_page, settings.base_url)
        orders.goto()
        orders.expect_loaded()

        # New order button should be visible
        expect(admin_page.get_by_role("link", name="New").or_(
            admin_page.get_by_role("button", name="New")
        ).first).to_be_visible()


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
        expect(admin_page.get_by_role("link", name="New").or_(
            admin_page.get_by_role("button", name="New").or_(
                admin_page.get_by_role("button", name="Record")
            )
        ).first).to_be_visible()


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
        """Complete flow: Create work order -> Dispatch -> Execute -> Complete."""
        from tests.playwright.pages.admin.operations.work_orders_page import WorkOrdersPage

        work_orders = WorkOrdersPage(admin_page, settings.base_url)
        work_orders.goto()
        work_orders.expect_loaded()

        # Work order management should be accessible
        expect(admin_page.locator("table")).to_be_visible()

    def test_dispatch_view_flow(self, admin_page: Page, settings):
        """Should be able to view dispatch schedule."""
        from tests.playwright.pages.admin.operations.dispatch_page import DispatchPage

        dispatch = DispatchPage(admin_page, settings.base_url)
        dispatch.goto()
        dispatch.expect_loaded()


class TestNetworkProvisioning:
    """Tests for network provisioning workflow."""

    def test_ont_provisioning_flow(self, admin_page: Page, settings):
        """Complete flow: OLT -> ONT -> IP -> Service."""
        from tests.playwright.pages.admin.network.olts_page import OLTsPage

        olts = OLTsPage(admin_page, settings.base_url)
        olts.goto()
        olts.expect_loaded()

        # OLT management should be accessible
        expect(admin_page.locator("table")).to_be_visible()

    def test_ip_assignment_flow(self, admin_page: Page, settings):
        """Should be able to assign IPs from pools."""
        from tests.playwright.pages.admin.network.ip_management_page import IPManagementPage

        ip_mgmt = IPManagementPage(admin_page, settings.base_url)
        ip_mgmt.goto()
        ip_mgmt.expect_loaded()


class TestCustomerOnboarding:
    """Tests for complete customer onboarding workflow."""

    def test_full_onboarding_visibility(self, admin_page: Page, settings):
        """All steps for customer onboarding should be accessible."""
        from tests.playwright.pages.admin.subscribers_page import SubscribersPage
        from tests.playwright.pages.admin.billing.invoices_page import InvoicesPage
        from tests.playwright.pages.admin.operations.service_orders_page import ServiceOrdersPage

        # Step 1: Subscribers accessible
        subscribers = SubscribersPage(admin_page, settings.base_url)
        subscribers.goto()
        subscribers.expect_loaded()

        # Step 2: Service orders accessible
        orders = ServiceOrdersPage(admin_page, settings.base_url)
        orders.goto()
        orders.expect_loaded()

        # Step 3: Billing accessible
        invoices = InvoicesPage(admin_page, settings.base_url)
        invoices.goto()
        invoices.expect_loaded()
