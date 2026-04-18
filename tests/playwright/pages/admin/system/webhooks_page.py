"""Webhooks page object."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.playwright.pages.admin.dashboard_page import AdminDashboardPage
from tests.playwright.pages.base_page import BasePage


class WebhooksPage(BasePage):
    """Page object for the webhooks list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/system/webhooks") -> None:
        """Navigate to the webhooks list."""
        dashboard = AdminDashboardPage(self.page, self.base_url)
        try:
            dashboard.goto()
            dashboard.expect_loaded()
            system_link = self.page.get_by_role("link", name="System").first
            if system_link.is_visible():
                system_link.click()
                self.page.wait_for_load_state("domcontentloaded")
            webhooks_link = self.page.get_by_role("link", name="Webhooks").first
            if webhooks_link.is_visible():
                webhooks_link.click()
                self.page.wait_for_load_state("domcontentloaded")
                return
        except PlaywrightError:
            pass

        last_error: Exception | None = None
        for _ in range(2):
            try:
                self.page.goto(
                    f"{self.base_url}{path}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                return
            except PlaywrightError as exc:
                last_error = exc
        if last_error:
            raise last_error

    def expect_loaded(self) -> None:
        """Assert the webhooks page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Webhooks", exact=True)
        ).to_be_visible()

    def click_new_webhook(self) -> None:
        """Click new webhook button."""
        self.page.get_by_role("link", name="New").first.click()

    def click_webhook_row(self, webhook_name: str) -> None:
        """Click on a webhook row."""
        self.page.get_by_role("row").filter(has_text=webhook_name).click()

    def expect_webhook_in_list(self, webhook_name: str) -> None:
        """Assert a webhook is visible in the list."""
        expect(
            self.page.get_by_role("row").filter(has_text=webhook_name)
        ).to_be_visible()

    def get_webhook_count(self) -> int:
        """Get the count of webhooks in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def toggle_webhook(self, webhook_name: str) -> None:
        """Toggle a webhook on/off."""
        row = self.page.get_by_role("row").filter(has_text=webhook_name)
        row.get_by_role("switch").click()
