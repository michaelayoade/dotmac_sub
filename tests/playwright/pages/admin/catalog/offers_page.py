"""Services (tariffs) list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class OffersPage(BasePage):
    """Page object for the catalog services list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/catalog") -> None:
        """Navigate to the services list."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the services page is loaded."""
        expect(self.page.get_by_role("heading", name="Services", exact=True)).to_be_visible()

    def search(self, query: str) -> None:
        """Search tariffs."""
        search_input = self.page.get_by_placeholder("Search tariffs...")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def click_new_offer(self) -> None:
        """Click new tariff button."""
        self.page.get_by_role("button", name="Add Tariff").click()

    def click_offer_row(self, offer_name: str) -> None:
        """Click on a tariff row."""
        self.page.get_by_role("row").filter(has_text=offer_name).click()

    def expect_offer_in_list(self, offer_name: str) -> None:
        """Assert a tariff is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=offer_name)).to_be_visible()

    def get_offer_count(self) -> int:
        """Get the count of tariffs in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def filter_by_status(self, status: str) -> None:
        """Filter by offer status."""
        self.page.get_by_label("Status").select_option(status)
