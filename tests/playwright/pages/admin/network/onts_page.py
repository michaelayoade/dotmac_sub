"""ONTs list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ONTsPage(BasePage):
    """Page object for the ONT devices list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
    def goto(self, path: str = "/admin/network/onts") -> None:
        """Navigate to the ONTs list."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the ONTs page is loaded."""
        expect(self.page.get_by_role("heading", name="ONT", exact=True)).to_be_visible()

    def search(self, query: str) -> None:
        """Search ONTs."""
        search_input = self.page.get_by_placeholder("Search")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def click_new_ont(self) -> None:
        """Click new ONT button."""
        self.page.get_by_role("link", name="New").first.click()

    def click_ont_row(self, serial: str) -> None:
        """Click on an ONT row by serial number."""
        self.page.get_by_role("row").filter(has_text=serial).click()

    def expect_ont_in_list(self, serial: str) -> None:
        """Assert an ONT is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=serial)).to_be_visible()

    def get_ont_count(self) -> int:
        """Get the count of ONTs in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def filter_by_status(self, status: str) -> None:
        """Filter by ONT status."""
        self.page.get_by_label("Status").select_option(status)
