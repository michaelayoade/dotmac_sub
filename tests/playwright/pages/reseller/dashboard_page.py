"""Reseller portal dashboard page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerDashboardPage(BasePage):
    """Page object for the reseller portal dashboard."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/dashboard") -> None:
        """Navigate to the reseller dashboard."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the dashboard is loaded."""
        expect(self.page.get_by_role("heading", name="Dashboard", exact=True)).to_be_visible()

    def expect_summary_visible(self) -> None:
        """Assert dashboard summary is visible."""
        expect(self.page.locator("[data-testid='summary']").or_(
            self.page.get_by_text("Total", exact=False).or_(
                self.page.get_by_text("Account", exact=False)
            )
        ).first).to_be_visible()

    def expect_accounts_visible(self) -> None:
        """Assert accounts section is visible."""
        expect(self.page.get_by_text("Account", exact=False).first).to_be_visible()

    def navigate_to_accounts(self) -> None:
        """Navigate to accounts page."""
        self.page.get_by_role("link", name="Account").first.click()

    def get_total_accounts(self) -> str:
        """Get total accounts count."""
        total_element = self.page.locator("[data-testid='total-accounts']").or_(
            self.page.get_by_text("Account", exact=False)
        ).first
        return total_element.text_content() or ""

    def get_active_accounts(self) -> str:
        """Get active accounts count."""
        active_element = self.page.locator("[data-testid='active-accounts']").or_(
            self.page.get_by_text("Active", exact=False)
        ).first
        return active_element.text_content() or ""

    def logout(self) -> None:
        """Log out of reseller portal."""
        self.page.get_by_role("button", name="Logout").or_(
            self.page.get_by_role("link", name="Logout")
        ).first.click()
