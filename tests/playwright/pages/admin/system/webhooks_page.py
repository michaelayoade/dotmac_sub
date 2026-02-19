"""Webhooks page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class WebhooksPage(BasePage):
    """Page object for the webhooks list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
    def goto(self, path: str = "/admin/system/webhooks") -> None:
        """Navigate to the webhooks list."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the webhooks page is loaded."""
        expect(self.page.get_by_role("heading", name="Webhooks", exact=True)).to_be_visible()

    def click_new_webhook(self) -> None:
        """Click new webhook button."""
        self.page.get_by_role("link", name="New").first.click()

    def click_webhook_row(self, webhook_name: str) -> None:
        """Click on a webhook row."""
        self.page.get_by_role("row").filter(has_text=webhook_name).click()

    def expect_webhook_in_list(self, webhook_name: str) -> None:
        """Assert a webhook is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=webhook_name)).to_be_visible()

    def get_webhook_count(self) -> int:
        """Get the count of webhooks in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def toggle_webhook(self, webhook_name: str) -> None:
        """Toggle a webhook on/off."""
        row = self.page.get_by_role("row").filter(has_text=webhook_name)
        row.get_by_role("switch").click()
