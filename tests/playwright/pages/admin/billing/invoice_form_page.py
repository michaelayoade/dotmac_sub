"""Invoice form page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class InvoiceFormPage(BasePage):
    """Page object for the invoice create/edit form."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto_new(self) -> None:
        """Navigate to the new invoice form."""
        super().goto("/admin/billing/invoices/new")

    def goto_edit(self, invoice_id: str) -> None:
        """Navigate to edit a specific invoice."""
        super().goto(f"/admin/billing/invoices/{invoice_id}/edit")

    def expect_loaded(self) -> None:
        """Assert the form is loaded."""
        expect(self.page.locator("form")).to_be_visible()

    def select_account(self, account_label: str) -> None:
        """Select a billing account."""
        self.page.get_by_label("Account").select_option(label=account_label)

    def fill_invoice_number(self, number: str) -> None:
        """Fill the invoice number."""
        self.page.get_by_label("Invoice Number").fill(number)

    def select_status(self, status: str) -> None:
        """Select invoice status."""
        self.page.get_by_label("Status").select_option(status)

    def fill_currency(self, currency: str) -> None:
        """Fill the currency field."""
        self.page.get_by_label("Currency").fill(currency)

    def fill_issued_date(self, date: str) -> None:
        """Fill the issued date (YYYY-MM-DD format)."""
        self.page.get_by_label("Issued").fill(date)

    def fill_due_date(self, date: str) -> None:
        """Fill the due date (YYYY-MM-DD format)."""
        self.page.get_by_label("Due").fill(date)

    def fill_memo(self, memo: str) -> None:
        """Fill the memo field."""
        self.page.get_by_label("Memo").fill(memo)

    def submit(self) -> None:
        """Submit the form."""
        self.page.get_by_role("button", name="Create Invoice").click()

    def cancel(self) -> None:
        """Cancel and go back."""
        self.page.get_by_role("link", name="Cancel").click()

    def expect_error(self, message: str) -> None:
        """Assert an error message is displayed."""
        expect(self.page.locator(".text-red-500, .text-red-700, .error").filter(has_text=message)).to_be_visible()

    def add_line_item(self, description: str, quantity: str, unit_price: str) -> None:
        """Add a line item to the invoice."""
        self.page.get_by_role("button", name="Add Line").click()
        # Fill line item fields
        self.page.get_by_placeholder("Description").last.fill(description)
        self.page.get_by_placeholder("Quantity").last.fill(quantity)
        self.page.get_by_placeholder("Unit Price").last.fill(unit_price)
