"""OLTs list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class OLTsPage(BasePage):
    """Page object for the OLT devices list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self) -> None:
        """Navigate to the OLTs list."""
        super().goto("/admin/network/olts")

    def expect_loaded(self) -> None:
        """Assert the OLTs page is loaded."""
        expect(self.page.get_by_role("heading", name="OLT", exact=True)).to_be_visible()

    def click_new_olt(self) -> None:
        """Click new OLT button."""
        self.page.get_by_role("link", name="New").first.click()

    def click_olt_row(self, olt_name: str) -> None:
        """Click on an OLT row."""
        self.page.get_by_role("row").filter(has_text=olt_name).click()

    def expect_olt_in_list(self, olt_name: str) -> None:
        """Assert an OLT is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=olt_name)).to_be_visible()

    def get_olt_count(self) -> int:
        """Get the count of OLTs in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def expect_stats_visible(self) -> None:
        """Assert stats are displayed."""
        expect(self.page.locator("[data-stat], .stat-card").first).to_be_visible()
