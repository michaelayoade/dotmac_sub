"""Payments list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class PaymentsPage(BasePage):
    """Page object for the payments list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/billing/payments") -> None:
        """Navigate to the payments list."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the payments page is loaded."""
        expect(self.page.get_by_role("heading", name="Payments", exact=True)).to_be_visible()

    def filter_by_status(self, status: str) -> None:
        """Filter payments by status."""
        self.page.get_by_label("Status").select_option(status)

    def search(self, query: str) -> None:
        """Search payments."""
        search_input = self.page.get_by_placeholder("Search")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def click_new_payment(self) -> None:
        """Click new payment button."""
        self.page.get_by_role("link", name="New Payment").click()

    def click_payment_row(self, reference: str) -> None:
        """Click on a payment row."""
        self.page.get_by_role("row").filter(has_text=reference).click()

    def expect_payment_in_list(self, reference: str) -> None:
        """Assert a payment is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=reference)).to_be_visible()

    def expect_no_payments(self) -> None:
        """Assert no payments message is shown."""
        expect(self.page.get_by_text("No payments")).to_be_visible()

    def get_payment_count(self) -> int:
        """Get the count of payments in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def get_total_amount(self) -> str | None:
        """Get total payments amount if displayed."""
        total = self.page.locator("[data-stat='total'], .total-payments").first
        if total.is_visible():
            return total.inner_text()
        return None
