"""OLTs list page object."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class OLTsPage(BasePage):
    """Page object for the OLT devices list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/network/olts") -> None:
        """Navigate to the OLTs list."""
        url = f"{self.base_url}{path}"
        timeout_ms = 120000
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError as exc:
            if "NS_ERROR_ABORT" not in str(exc):
                raise
            self.page.wait_for_timeout(500)
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def expect_loaded(self) -> None:
        """Assert the OLTs page is loaded."""
        expect(
            self.page.get_by_role("heading", name="OLT Devices", exact=True)
        ).to_be_visible()

    def click_new_olt(self) -> None:
        """Click new OLT button."""
        self.page.get_by_role("link", name="Add OLT").click()

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
