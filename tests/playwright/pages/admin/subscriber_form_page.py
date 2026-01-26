"""Subscriber form page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class SubscriberFormPage(BasePage):
    """Page object for the subscriber create/edit form."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto_new(self) -> None:
        """Navigate to the new subscriber form."""
        super().goto("/admin/subscribers/new")

    def goto_edit(self, subscriber_id: str) -> None:
        """Navigate to edit a specific subscriber."""
        super().goto(f"/admin/subscribers/{subscriber_id}/edit")

    def expect_loaded(self) -> None:
        """Assert the form is loaded."""
        expect(self.page.locator("form")).to_be_visible()

    def expect_create_mode(self) -> None:
        """Assert form is in create mode."""
        expect(self.page.get_by_role("heading", name="New Subscriber", exact=True)).to_be_visible()

    def expect_edit_mode(self) -> None:
        """Assert form is in edit mode."""
        expect(self.page.get_by_role("heading", name="Edit Subscriber", exact=True)).to_be_visible()

    def search_customer(self, query: str) -> None:
        """Search for a customer (person or organization)."""
        search_input = self.page.get_by_placeholder("Search customers")
        if search_input.is_visible():
            search_input.fill(query)
            # Wait for search results
            self.page.wait_for_timeout(500)

    def select_customer_result(self, name: str) -> None:
        """Select a customer from search results."""
        self.page.get_by_role("option", name=name).click()

    def select_subscriber_type(self, subscriber_type: str) -> None:
        """Select subscriber type (person/organization)."""
        self.page.get_by_label("Subscriber Type").select_option(subscriber_type)

    def fill_subscriber_number(self, number: str) -> None:
        """Fill the subscriber number field."""
        self.page.get_by_label("Subscriber Number").fill(number)

    def fill_notes(self, notes: str) -> None:
        """Fill the notes field."""
        self.page.get_by_label("Notes").fill(notes)

    def set_active(self, active: bool) -> None:
        """Set the active checkbox."""
        checkbox = self.page.get_by_label("Active")
        if active:
            checkbox.check()
        else:
            checkbox.uncheck()

    def submit(self) -> None:
        """Submit the form."""
        self.page.get_by_role("button", name="Save").click()

    def cancel(self) -> None:
        """Cancel and go back."""
        self.page.get_by_role("link", name="Cancel").click()

    def expect_error(self, message: str) -> None:
        """Assert an error message is displayed."""
        expect(self.page.locator(".text-red-500, .text-red-700, .error").filter(has_text=message)).to_be_visible()

    def expect_validation_error(self, field: str) -> None:
        """Assert a field has a validation error."""
        field_locator = self.page.get_by_label(field)
        expect(field_locator).to_have_attribute("aria-invalid", "true")
