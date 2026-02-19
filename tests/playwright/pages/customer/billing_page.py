"""Customer portal billing page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class CustomerBillingPage(BasePage):
    """Page object for the customer billing page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/customer/billing") -> None:
        """Navigate to the billing page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the billing page is loaded."""
        expect(self.page.get_by_role("heading", name="Billing", exact=True)).to_be_visible()

    def expect_invoices_visible(self) -> None:
        """Assert invoices section is visible."""
        expect(self.page.get_by_text("Invoice", exact=False).first).to_be_visible()

    def expect_payment_history_visible(self) -> None:
        """Assert payment history is visible."""
        expect(self.page.get_by_text("Payment", exact=False).first).to_be_visible()

    def expect_balance_visible(self) -> None:
        """Assert account balance is visible."""
        expect(self.page.locator("[data-testid='current-balance']").or_(
            self.page.get_by_text("Balance", exact=False)
        ).first).to_be_visible()

    def click_view_invoice(self, invoice_number: str) -> None:
        """Click to view a specific invoice."""
        row = self.page.get_by_role("row").filter(has_text=invoice_number)
        row.get_by_role("link", name="View").or_(
            row.get_by_role("button", name="View")
        ).first.click()

    def click_pay_invoice(self, invoice_number: str) -> None:
        """Click to pay a specific invoice."""
        row = self.page.get_by_role("row").filter(has_text=invoice_number)
        row.get_by_role("button", name="Pay").first.click()

    def click_make_payment(self) -> None:
        """Click make payment button."""
        self.page.get_by_role("button", name="Make Payment").or_(
            self.page.get_by_role("link", name="Make Payment")
        ).first.click()

    def get_invoice_count(self) -> int:
        """Get the count of invoices displayed."""
        rows = self.page.locator("table tbody tr")
        return rows.count()

    def expect_invoice_in_list(self, invoice_number: str) -> None:
        """Assert an invoice is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=invoice_number)).to_be_visible()

    def download_invoice(self, invoice_number: str) -> None:
        """Download an invoice PDF."""
        row = self.page.get_by_role("row").filter(has_text=invoice_number)
        row.get_by_role("link", name="Download").or_(
            row.get_by_role("button", name="Download")
        ).first.click()

    def view_payment_methods(self) -> None:
        """View payment methods."""
        self.page.get_by_role("link", name="Payment Methods").or_(
            self.page.get_by_role("button", name="Payment Methods")
        ).first.click()

    def enable_autopay(self) -> None:
        """Enable automatic payments."""
        self.page.get_by_role("button", name="Auto").or_(
            self.page.get_by_label("Auto")
        ).first.click()
