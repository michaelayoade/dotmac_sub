"""Customer portal dashboard page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class CustomerDashboardPage(BasePage):
    """Page object for the customer portal dashboard."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self) -> None:
        """Navigate to the customer dashboard."""
        super().goto("/customer/dashboard")

    def expect_loaded(self) -> None:
        """Assert the dashboard is loaded."""
        expect(self.page.get_by_role("heading", name="Dashboard", exact=True)).to_be_visible()

    def expect_account_summary_visible(self) -> None:
        """Assert account summary section is visible."""
        expect(self.page.locator("[data-testid='account-summary']").or_(
            self.page.get_by_text("Account", exact=False)
        ).first).to_be_visible()

    def expect_service_status_visible(self) -> None:
        """Assert service status section is visible."""
        expect(self.page.locator("[data-testid='service-status']").or_(
            self.page.get_by_text("Service", exact=False)
        ).first).to_be_visible()

    def expect_billing_summary_visible(self) -> None:
        """Assert billing summary section is visible."""
        expect(self.page.locator("[data-testid='billing-summary']").or_(
            self.page.get_by_text("Balance", exact=False).or_(
                self.page.get_by_text("Billing", exact=False)
            )
        ).first).to_be_visible()

    def navigate_to_billing(self) -> None:
        """Navigate to billing page."""
        self.page.get_by_role("link", name="Billing").first.click()

    def navigate_to_services(self) -> None:
        """Navigate to services page."""
        self.page.get_by_role("link", name="Services").first.click()

    def navigate_to_support(self) -> None:
        """Navigate to support page."""
        self.page.get_by_role("link", name="Support").first.click()

    def navigate_to_profile(self) -> None:
        """Navigate to profile page."""
        self.page.get_by_role("link", name="Profile").first.click()

    def get_account_balance(self) -> str:
        """Get account balance text."""
        balance_element = self.page.locator("[data-testid='account-balance']").or_(
            self.page.get_by_text("$").first
        )
        return balance_element.text_content() or ""

    def logout(self) -> None:
        """Log out of customer portal."""
        self.page.get_by_role("button", name="Logout").or_(
            self.page.get_by_role("link", name="Logout")
        ).first.click()
