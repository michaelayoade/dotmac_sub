"""Reseller portal accounts page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerAccountsPage(BasePage):
    """Page object for the reseller accounts page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/accounts") -> None:
        """Navigate to the accounts page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the accounts page is loaded."""
        expect(self.page.get_by_role("heading", name="Account", exact=True)).to_be_visible()

    def get_account_count(self) -> int:
        """Get count of accounts displayed."""
        rows = self.page.locator("table tbody tr")
        return rows.count()

    def expect_account_in_list(self, account_name: str) -> None:
        """Assert an account is visible in the list."""
        expect(self.page.get_by_text(account_name)).to_be_visible()

    def search_accounts(self, query: str) -> None:
        """Search accounts."""
        search_input = self.page.get_by_placeholder("Search")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def view_account(self, account_name: str) -> None:
        """View an account (impersonate)."""
        row = self.page.get_by_role("row").filter(has_text=account_name)
        row.get_by_role("button", name="View").or_(
            row.get_by_role("link", name="View")
        ).first.click()

    def filter_by_status(self, status: str) -> None:
        """Filter accounts by status."""
        self.page.get_by_label("Status").select_option(status)

    def go_to_page(self, page_number: int) -> None:
        """Navigate to a specific page."""
        self.page.get_by_role("link", name=str(page_number)).click()

    def next_page(self) -> None:
        """Go to next page."""
        self.page.get_by_role("link", name="Next").or_(
            self.page.get_by_role("button", name="Next")
        ).first.click()

    def previous_page(self) -> None:
        """Go to previous page."""
        self.page.get_by_role("link", name="Previous").or_(
            self.page.get_by_role("button", name="Previous")
        ).first.click()
