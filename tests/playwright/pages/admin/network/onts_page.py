"""ONTs list page object."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ONTsPage(BasePage):
    """Page object for the ONT devices list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/network/onts") -> None:
        """Navigate to the ONTs list."""
        url = f"{self.base_url}{path}"
        timeout_ms = 180000
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError as exc:
            if "NS_ERROR_ABORT" not in str(exc):
                raise
            self.page.wait_for_timeout(500)
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def expect_loaded(self) -> None:
        """Assert the ONTs page is loaded."""
        expect(
            self.page.get_by_role("heading", name="ONT Fleet", exact=True)
        ).to_be_visible()

    def search(self, query: str) -> None:
        """Search ONTs."""
        search_input = self.page.get_by_placeholder("Search")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def click_new_ont(self) -> None:
        """Click new ONT button."""
        self.page.get_by_role("link", name="Add ONT").click()

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
