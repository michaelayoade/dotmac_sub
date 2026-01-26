"""API Keys list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class APIKeysPage(BasePage):
    """Page object for the API keys list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self) -> None:
        """Navigate to the API keys list."""
        super().goto("/admin/system/api-keys")

    def expect_loaded(self) -> None:
        """Assert the API keys page is loaded."""
        expect(self.page.get_by_role("heading", name="API Keys", exact=True)).to_be_visible()

    def click_new_api_key(self) -> None:
        """Click new API key button."""
        self.page.get_by_role("link", name="New").first.click()

    def click_key_row(self, key_name: str) -> None:
        """Click on an API key row."""
        self.page.get_by_role("row").filter(has_text=key_name).click()

    def expect_key_in_list(self, key_name: str) -> None:
        """Assert an API key is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=key_name)).to_be_visible()

    def revoke_key(self, key_name: str) -> None:
        """Revoke an API key."""
        row = self.page.get_by_role("row").filter(has_text=key_name)
        row.get_by_role("button", name="Revoke").click()

    def get_key_count(self) -> int:
        """Get the count of API keys in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()
