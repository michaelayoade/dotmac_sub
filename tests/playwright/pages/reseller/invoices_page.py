"""Reseller portal invoices page objects."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerAccountInvoicesPage(BasePage):
    """Page object for the reseller account invoices list."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, account_id: str) -> None:
        """Navigate to an account's invoices list."""
        super().goto(f"/reseller/accounts/{account_id}/invoices")

    def expect_loaded(self) -> None:
        """Assert the invoices page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Invoices", exact=True)
        ).to_be_visible()

    def invoice_view_links(self):
        """Locator for the per-invoice 'View' links."""
        return self.page.locator("a[href*='/invoices/']")

    def has_invoices(self) -> bool:
        """Whether at least one invoice row is rendered."""
        return self.invoice_view_links().count() > 0

    def open_first_invoice(self) -> None:
        """Open the first invoice detail page."""
        self.invoice_view_links().first.click()


class ResellerInvoiceDetailPage(BasePage):
    """Page object for the reseller invoice detail page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def expect_loaded(self) -> None:
        """Assert the invoice detail page is loaded."""
        # The heading reads "Invoice <number>"; match the prefix.
        expect(
            self.page.get_by_role("heading", name="Invoice", exact=False).first
        ).to_be_visible()
        expect(self.page.get_by_role("heading", name="Payments")).to_be_visible()
