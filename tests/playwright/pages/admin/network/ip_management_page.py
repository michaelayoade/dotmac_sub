"""IP Management page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class IPManagementPage(BasePage):
    """Page object for the IP management page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self) -> None:
        """Navigate to the IP management page."""
        super().goto("/admin/network/ip-management")

    def expect_loaded(self) -> None:
        """Assert the IP management page is loaded."""
        expect(self.page.get_by_role("heading", name="IP", exact=True)).to_be_visible()

    def click_new_pool(self) -> None:
        """Click new IP pool button."""
        self.page.get_by_role("link", name="New Pool").click()

    def click_pool_row(self, pool_name: str) -> None:
        """Click on an IP pool row."""
        self.page.get_by_role("row").filter(has_text=pool_name).click()

    def expect_pool_in_list(self, pool_name: str) -> None:
        """Assert an IP pool is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=pool_name)).to_be_visible()

    def get_pool_count(self) -> int:
        """Get the count of IP pools in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def expect_utilization_visible(self) -> None:
        """Assert IP utilization stats are visible."""
        expect(self.page.get_by_text("Utilization")).to_be_visible()
