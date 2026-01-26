"""Subscribers list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class SubscribersPage(BasePage):
    """Page object for the subscribers list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self) -> None:
        """Navigate to the subscribers list."""
        super().goto("/admin/subscribers")

    def expect_loaded(self) -> None:
        """Assert the subscribers page is loaded."""
        expect(self.page.get_by_role("heading", name="Subscribers", exact=True)).to_be_visible()

    def search(self, query: str) -> None:
        """Search for subscribers."""
        search_input = self.page.get_by_placeholder("Search by name, email, or ID")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def filter_by_type(self, subscriber_type: str) -> None:
        """Filter by subscriber type (person/organization)."""
        self.page.get_by_label("Type").select_option(subscriber_type)

    def click_new_subscriber(self) -> None:
        """Click the new subscriber button."""
        self.page.get_by_role("link", name="Add Subscriber").first.click()

    def click_subscriber_row(self, identifier: str) -> None:
        """Click on a subscriber row by name or number."""
        self.page.get_by_role("row").filter(has_text=identifier).click()

    def expect_subscriber_in_list(self, identifier: str) -> None:
        """Assert a subscriber is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=identifier)).to_be_visible()

    def expect_no_results(self) -> None:
        """Assert no results are shown."""
        expect(self.page.get_by_text("No subscribers found")).to_be_visible()

    def get_subscriber_count(self) -> int:
        """Get the count of subscribers in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def click_page(self, page_num: int) -> None:
        """Click a pagination page number."""
        self.page.get_by_role("link", name=str(page_num)).click()

    def expect_page_active(self, page_num: int) -> None:
        """Assert a pagination page is active."""
        expect(self.page.locator(f"[aria-current='page']").filter(has_text=str(page_num))).to_be_visible()
